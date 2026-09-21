"""MQTT adapter lifecycle, driven through a fake transport.

BACKEND_SPEC requires the no-broker verification path to cover the adapter
itself, not only the ingestion use case behind it. Every test here runs the
real :class:`PahoMqttAdapter`: connect handling, subscription results, the
bounded ingress queue, acknowledgement, saturation, redelivery and shutdown
ordering. Only the Paho client underneath is fake.

What these tests cannot prove is listed in docs/TESTING.md: real QoS 1
redelivery timing, a real CONNACK and a real broker session are NOT_RUN.
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Callable

import pytest
from paho.mqtt import __version__ as paho_version

from powerguard.config import Settings
from powerguard.mqtt.client import (
    RECONNECT_MAX_S,
    MqttTransport,
    PahoMqttAdapter,
    _build_paho_transport,
    granted_qos,
    reason_is_failure,
)
from powerguard.mqtt.ports import AckDecision, InboundMessage
from powerguard.mqtt.topics import SUBSCRIPTIONS, telemetry_topic
from tests.builders import DEVICE_ID
from tests.conftest import make_settings
from tests.fakes.fake_transport import (
    MQTT_ERR_NO_CONN,
    SUBACK_FAILURE,
    FakeMqttTransport,
    FakeReasonCode,
)

PAYLOAD = b'{"schema_version":1}'


class RecordingHandler:
    """Returns a scripted decision and records what it was asked to handle."""

    def __init__(self, decision: AckDecision = AckDecision.ACK) -> None:
        self.decision = decision
        self.seen: list[InboundMessage] = []
        self.failure: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, message: InboundMessage) -> AckDecision:
        if self.gate is not None:
            await self.gate.wait()
        self.seen.append(message)
        if self.failure is not None:
            raise self.failure
        return self.decision


@pytest.fixture
def mqtt_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="never-logged",
        mqtt_ingress_queue_size=2,
        mqtt_shutdown_grace_s=2.0,
    )


@pytest.fixture
def transport() -> FakeMqttTransport:
    return FakeMqttTransport()


def build_adapter(
    settings: Settings,
    handler: RecordingHandler,
    transport: FakeMqttTransport,
    connection_log: list[bool] | None = None,
    suback_timeout_s: float = 30.0,
) -> PahoMqttAdapter:
    return PahoMqttAdapter(
        settings,
        handler,
        on_connection_change=(connection_log.append if connection_log is not None else None),
        transport_factory=lambda _s: transport,
        # Deterministic jitter so the delay assertions are exact.
        rng=random.Random(20260921),
        suback_timeout_s=suback_timeout_s,
    )


class ScriptedRandom(random.Random):
    """A Random whose `random()` returns a fixed sequence, then repeats the last.

    Makes every jitter window checkable by value instead of only by range.
    """

    def __init__(self, values: list[float]) -> None:
        super().__init__()
        self._values = list(values)
        self._index = 0

    def random(self) -> float:
        if self._index < len(self._values):
            value = self._values[self._index]
            self._index += 1
            return value
        return self._values[-1] if self._values else 0.0


async def connected(transport: FakeMqttTransport, timeout: float = 2.0) -> None:
    """Wait for the scheduler's connection attempt to reach the transport.

    `start()` no longer connects inline: the first attempt goes through the same
    scheduler as every later one, and its blocking part runs in a worker thread.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while transport.loop_started == 0:
        if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
            raise AssertionError("the adapter never started its network loop")
        await asyncio.sleep(0.005)


def pending_delay(adapter: PahoMqttAdapter) -> float:
    """Seconds until the armed reconnect attempt fires."""
    timer = adapter._reconnect_timer
    assert timer is not None, "no reconnect is armed"
    return float(timer.when() - asyncio.get_event_loop().time())


async def wait_until(
    predicate: Callable[[], bool], timeout: float = 2.0
) -> None:
    """Poll until a condition set by a worker thread becomes true."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
            raise AssertionError("condition never held")
        await asyncio.sleep(0.005)


async def settle() -> None:
    """Let the adapter's drain task run to completion of pending work."""
    for _ in range(5):
        await asyncio.sleep(0)


# -- connect ---------------------------------------------------------------


async def test_start_connects_and_subscribes_at_qos_1(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    # Enforced by the adapter itself, before anything is started.
    assert transport.manual_ack is True

    await adapter.start()
    try:
        assert transport.connect_calls == [
            (mqtt_settings.mqtt_host, mqtt_settings.mqtt_port, mqtt_settings.mqtt_keepalive_s)
        ]
        await connected(transport)
        assert transport.loop_started == 1
        # Not connected until the broker says so.
        assert connection_log == []

        transport.connect_and_subscribe()
        assert transport.subscriptions == list(SUBSCRIPTIONS)
        assert connection_log == [True]
        assert adapter.counters.connected == 1
    finally:
        await adapter.stop()


async def test_refused_connack_is_never_reported_as_connected(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)

    await adapter.start()
    try:
        transport.complete_connect(reason=5)  # not authorised
        assert transport.subscriptions == []
        assert connection_log == [False]
        assert adapter.counters.connected == 0
        assert adapter.counters.connect_failures == 1
    finally:
        await adapter.stop()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (0, False),
        (5, True),
        (None, True),
        ("not-a-reason-code", True),
    ],
)
def test_connack_reason_is_only_success_when_it_says_so(
    reason: object, expected: bool
) -> None:
    assert reason_is_failure(reason) is expected


async def test_failed_subscription_drops_the_useless_session(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    transport.subscribe_result = MQTT_ERR_NO_CONN
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)

    await adapter.start()
    try:
        transport.complete_connect()
        # Connected but deaf: never advertised as healthy, and re-established.
        assert connection_log == [False, False]
        # The first rejection ends the attempt: there is no point queueing the
        # rest of a subscription set that cannot be completed.
        assert adapter.counters.subscribe_failures == 1
        assert adapter.counters.connected == 0
        assert adapter.counters.reconnects_requested == 1
    finally:
        await adapter.stop()


# -- message flow ----------------------------------------------------------


async def test_delivery_is_acknowledged_only_after_the_handler_decides(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler(AckDecision.ACK)
    handler.gate = asyncio.Event()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        mid = transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
        await settle()
        # The handler is still deciding, so nothing has been acknowledged.
        assert transport.acks == []

        handler.gate.set()
        await settle()
        assert transport.acks == [(mid, 1)]
        assert handler.seen[0].topic == telemetry_topic(DEVICE_ID)
        assert handler.seen[0].payload == PAYLOAD
        assert handler.seen[0].received_at.tzinfo is not None
        assert adapter.counters.acknowledged == 1
    finally:
        handler.gate.set()
        await adapter.stop()


async def test_retained_flag_reaches_the_handler(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        transport.deliver("powerguard/v1/devices/x/status", PAYLOAD, retain=True)
        await settle()
        assert handler.seen[0].retained is True
    finally:
        await adapter.stop()


async def test_withheld_delivery_is_not_acked_and_forces_redelivery(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler(AckDecision.WITHHOLD)
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
        await settle()

        assert transport.acks == []
        assert adapter.counters.withheld == 1
        # A QoS 1 delivery is only re-sent after the session drops, so the
        # adapter must actively drop it instead of waiting forever.
        assert adapter.counters.reconnects_requested == 1
        assert adapter.counters.reconnects_scheduled == 1
        assert transport.disconnects == 1
    finally:
        await adapter.stop()


async def test_repeated_withholding_does_not_storm_the_broker(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler(AckDecision.WITHHOLD)
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        for _ in range(3):
            transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
            await settle()

        assert adapter.counters.withheld == 3
        # One drop per session, not one per message.
        assert adapter.counters.reconnects_requested == 1

        # The next session re-arms it.
        transport.connect_and_subscribe()
        transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
        await settle()
        assert adapter.counters.reconnects_requested == 2
    finally:
        await adapter.stop()


async def test_handler_exception_is_isolated_and_withholds(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    handler.failure = RuntimeError("boom")
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
        await settle()

        assert transport.acks == []
        assert adapter.counters.handler_errors == 1
        assert adapter.counters.reconnects_requested == 1

        # The drain task survived, so the next delivery is still handled.
        handler.failure = None
        mid = transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
        await settle()
        assert transport.acks == [(mid, 1)]
    finally:
        await adapter.stop()


# -- saturation ------------------------------------------------------------


async def test_saturated_queue_never_acknowledges_and_requests_redelivery(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    handler.gate = asyncio.Event()  # nothing is consumed while the gate is shut
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        # Queue size is 2; the drain task takes one, so 4 deliveries overflow.
        mids = [transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD) for _ in range(4)]
        await settle()

        assert adapter.counters.saturated >= 1
        assert adapter.counters.enqueued < len(mids)
        assert transport.acks == [], "an unhandled delivery is never acknowledged"
        assert adapter.counters.reconnects_requested >= 1
    finally:
        handler.gate.set()
        await adapter.stop()


# -- reconnect backoff -----------------------------------------------------


async def test_reconnect_delay_is_capped_exponential_with_jitter(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)

    delays = [adapter.reconnect_delay() for _ in range(9)]
    bases = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    for delay, base in zip(delays, bases, strict=True):
        assert base / 2.0 <= delay <= base
        assert delay <= RECONNECT_MAX_S
    # Jitter means consecutive delays at the cap are not identical.
    assert len(set(delays[-3:])) > 1


async def test_successful_connect_resets_the_backoff(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        adapter.request_reconnect("first")
        adapter.request_reconnect("second")
        transport.connect_and_subscribe()
        assert adapter.reconnect_delay() <= 1.0
    finally:
        await adapter.stop()


async def test_disconnect_marks_the_adapter_not_connected(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        transport.drop_connection()
        assert connection_log == [True, False]
        assert adapter.counters.disconnected == 1
    finally:
        await adapter.stop()


# -- shutdown --------------------------------------------------------------


async def test_shutdown_unsubscribes_drains_then_disconnects(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    transport.connect_and_subscribe()
    mid = transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
    await settle()  # the delivery is queued before shutdown begins

    await adapter.stop()

    assert transport.unsubscriptions == list(SUBSCRIPTIONS)
    assert transport.acks == [(mid, 1)], "accepted work is acknowledged before leaving"
    # The acknowledgement must be sent while the network thread is still alive.
    assert transport.trace.index(f"ack:{mid}") < transport.trace.index("disconnect")
    assert transport.trace.index("disconnect") < transport.trace.index("loop_stop")
    assert transport.loop_stopped == 1


async def test_shutdown_gives_up_after_the_grace_period(
    database_url: str, transport: FakeMqttTransport
) -> None:
    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="never-logged",
        mqtt_shutdown_grace_s=0.05,
    )
    handler = RecordingHandler()
    handler.gate = asyncio.Event()  # never opened: the drain cannot finish
    adapter = build_adapter(settings, handler, transport)
    await adapter.start()
    await connected(transport)
    transport.connect_and_subscribe()
    transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
    await settle()  # queued, then stuck in the handler

    await adapter.stop()

    assert adapter.counters.drain_timeouts == 1
    assert transport.acks == [], "an undecided delivery is never acknowledged"
    assert transport.loop_stopped == 1
    handler.gate.set()


async def test_shutdown_does_not_request_a_reconnect(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler(AckDecision.WITHHOLD)
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    transport.connect_and_subscribe()
    await adapter.stop()

    connects_before = len(transport.connect_calls)
    adapter.request_reconnect("after shutdown")
    assert len(transport.connect_calls) == connects_before


def test_default_transport_is_a_persistent_authenticated_session(
    mqtt_settings: Settings,
) -> None:
    """The real Paho client, configured but never connected."""
    client = _build_paho_transport(mqtt_settings)

    assert client._client_id == b"powerguard-backend-local"
    assert client._clean_session is False
    assert client._username == b"backend"
    # The secret is handed to the client, and never to a log line or a repr.
    assert "never-logged" not in repr(mqtt_settings)
    assert "never-logged" not in str(mqtt_settings.redacted())


# -- SUBACK: only the broker makes a subscription active --------------------


async def test_local_subscribe_success_is_not_a_subscription(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """`subscribe()` returning 0 only means the packet was queued locally.

    Treating it as confirmation would report a healthy session while the broker
    had not agreed to send anything.
    """
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.complete_connect()

        assert transport.subscriptions == list(SUBSCRIPTIONS)
        assert len(transport.pending_subacks) == len(SUBSCRIPTIONS)
        assert connection_log == [], "no SUBACK yet, so no connection to report"
        assert adapter.counters.connected == 0
        assert adapter.subscriptions_confirmed is False
    finally:
        await adapter.stop()


async def test_connection_is_reported_only_after_every_suback(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.complete_connect()
        mids = list(transport.pending_subacks)
        assert len(mids) == 2

        transport.send_suback(mids[0], granted=1)
        assert connection_log == [], "one filter confirmed is not the whole session"
        assert adapter.counters.subscribed == 1
        assert adapter.counters.connected == 0

        transport.send_suback(mids[1], granted=1)
        assert connection_log == [True]
        assert adapter.counters.subscribed == 2
        assert adapter.counters.connected == 1
        assert adapter.subscriptions_confirmed is True
    finally:
        await adapter.stop()


@pytest.mark.parametrize("granted", [SUBACK_FAILURE, 0])
async def test_a_rejected_or_downgraded_suback_drops_the_session(
    mqtt_settings: Settings, transport: FakeMqttTransport, granted: int
) -> None:
    """0x80 is a refusal; granted QoS 0 removes the redelivery the design needs."""
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.complete_connect()
        mids = list(transport.pending_subacks)
        transport.send_suback(mids[0], granted=granted)

        assert adapter.counters.subscriptions_refused == 1
        assert adapter.counters.connected == 0
        assert adapter.subscriptions_confirmed is False
        assert True not in connection_log
        assert adapter.counters.reconnects_requested == 1
        assert transport.disconnects == 1
    finally:
        await adapter.stop()


async def test_no_suback_at_all_times_out_and_retries(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """A broker that accepts CONNECT and never answers SUBSCRIBE."""
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(
        mqtt_settings, handler, transport, connection_log, suback_timeout_s=0.05
    )
    await adapter.start()
    try:
        transport.complete_connect()
        assert adapter.counters.connected == 0

        await asyncio.sleep(0.12)

        assert adapter.counters.suback_timeouts == 1
        assert adapter.counters.connected == 0
        assert True not in connection_log
        assert adapter.counters.reconnects_requested == 1
        assert adapter.counters.sessions_failed == 1
    finally:
        await adapter.stop()


async def test_a_suback_that_arrives_in_time_cancels_the_timeout(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport, suback_timeout_s=0.05)
    await adapter.start()
    try:
        transport.connect_and_subscribe()
        await asyncio.sleep(0.12)

        assert adapter.counters.suback_timeouts == 0
        assert adapter.counters.connected == 1
        assert adapter.counters.reconnects_requested == 0
    finally:
        await adapter.stop()


async def test_a_disconnect_before_the_suback_clears_the_pending_state(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(
        mqtt_settings, handler, transport, connection_log, suback_timeout_s=0.05
    )
    await adapter.start()
    try:
        transport.complete_connect()
        transport.drop_connection()

        assert adapter.counters.connected == 0
        assert adapter.subscriptions_confirmed is False
        assert True not in connection_log

        # The timer died with the session, so no timeout fires for it later.
        await asyncio.sleep(0.12)
        assert adapter.counters.suback_timeouts == 0
        assert adapter.counters.connected == 0
    finally:
        await adapter.stop()


async def test_a_stale_suback_from_a_previous_session_is_ignored(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        transport.complete_connect()
        stale_mid = next(iter(transport.pending_subacks))
        transport.drop_connection()

        transport.send_suback(stale_mid, granted=1)

        assert adapter.counters.subscribed == 0
        assert adapter.counters.connected == 0
    finally:
        await adapter.stop()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, 0),
        (1, 1),
        (2, 2),
        (SUBACK_FAILURE, None),
        (None, None),
        ("nonsense", None),
        (FakeReasonCode(1), 1),
        (FakeReasonCode(SUBACK_FAILURE), None),
    ],
)
def test_granted_qos_reads_a_suback_entry_not_a_connack_reason(
    code: object, expected: int | None
) -> None:
    """A SUBACK entry of 1 is success; a CONNACK reason of 1 is a failure."""
    assert granted_qos(code) == expected


# -- unexpected disconnect and shutdown intake ------------------------------


async def test_an_unexpected_disconnect_uses_the_jitter_policy(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Paho would otherwise retry on its own plain exponential, with no jitter."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        await connected(transport)
        transport.connect_and_subscribe()
        scheduled_before = adapter.counters.reconnects_scheduled

        transport.drop_connection()
        await settle()

        assert adapter.counters.unexpected_disconnects == 1
        # Scheduled by the adapter, which is the only component that schedules.
        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
        assert adapter.counters.reconnects_requested == 0
    finally:
        await adapter.stop()


async def test_repeated_unexpected_disconnects_back_off(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        delays: list[float] = []
        for index in range(4):
            transport.drop_connection()
            await settle()
            delay = pending_delay(adapter)
            delays.append(delay)
            base = min(1.0 * (2**index), RECONNECT_MAX_S)
            assert base / 2.0 <= delay <= base
            # Hold the sequence: cancel the armed attempt so the next drop
            # schedules rather than being coalesced into this one.
            adapter._cancel_reconnect_timer()

        # A confirmed session resets the growth.
        transport.connect_and_subscribe()
        transport.drop_connection()
        await settle()
        assert 0.5 <= pending_delay(adapter) <= 1.0
    finally:
        await adapter.stop()


async def test_a_disconnect_during_shutdown_schedules_nothing(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    transport.connect_and_subscribe()
    await adapter.stop()
    scheduled = adapter.counters.reconnects_scheduled

    transport.drop_connection()

    assert adapter.counters.unexpected_disconnects == 0
    assert adapter.counters.reconnects_scheduled == scheduled


async def test_messages_arriving_after_shutdown_begins_are_refused(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Refused, not acknowledged: the broker still owns them."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    transport.connect_and_subscribe()
    await adapter.stop()

    enqueued_before = adapter.counters.enqueued
    for _ in range(3):
        transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
    await settle()

    assert adapter.counters.dropped_after_shutdown == 3
    assert adapter.counters.enqueued == enqueued_before
    assert handler.seen == []
    assert transport.acks == []


async def test_a_delivery_racing_the_start_of_shutdown_is_not_enqueued(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """The network callback ran before shutdown; the loop tick lands after it."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    transport.connect_and_subscribe()

    # Delivered, but its enqueue callback has not run yet.
    transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
    await adapter.stop()
    await settle()

    assert adapter.counters.enqueued == 0
    assert adapter.counters.dropped_after_shutdown == 1
    assert transport.acks == []


# -- a subscription session fails as a whole -------------------------------


async def test_a_rejected_suback_kills_the_whole_session(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """The regression Codex asked for.

    Subscription A is refused while B is still pending. B's SUBACK then arrives
    and accepts. The session must stay dead: it was already unusable when A was
    refused, and half a subscription set is not a working backend.
    """
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.complete_connect()
        mid_a, mid_b = list(transport.pending_subacks)

        transport.send_suback(mid_a, granted=SUBACK_FAILURE)

        assert adapter.counters.subscriptions_refused == 1
        assert adapter.counters.sessions_failed == 1
        # Nothing is left waiting: B was abandoned with A.
        assert adapter._pending_subacks == {}

        # B answers late, and accepts. It must change nothing.
        transport.send_suback(mid_b, granted=1)

        assert adapter.counters.connected == 0
        assert adapter.subscriptions_confirmed is False
        assert True not in connection_log
        assert adapter.counters.stale_subacks_ignored == 1
    finally:
        await adapter.stop()


async def test_a_session_that_failed_on_timeout_cannot_be_revived(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(
        mqtt_settings, handler, transport, connection_log, suback_timeout_s=0.05
    )
    await adapter.start()
    try:
        transport.complete_connect()
        mids = list(transport.pending_subacks)
        await asyncio.sleep(0.12)
        assert adapter.counters.suback_timeouts == 1

        for mid in mids:
            transport.send_suback(mid, granted=1)

        assert adapter.counters.connected == 0
        assert True not in connection_log
        assert adapter.counters.stale_subacks_ignored == len(mids)
    finally:
        await adapter.stop()


async def test_a_stale_suback_after_a_reconnect_cannot_confirm_the_new_session(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Brokers reuse message ids, so a mid alone proves nothing.

    The old session's mid is replayed after a reconnect has started a new one.
    Only the generation tells them apart.
    """
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.complete_connect()
        old_mids = list(transport.pending_subacks)
        transport.drop_connection()

        # A new session, whose mids happen to collide with the old ones.
        transport.complete_connect()
        transport.pending_subacks = dict(
            zip(old_mids, transport.pending_subacks.values(), strict=True)
        )

        for mid in old_mids:
            transport.send_suback(mid, granted=1)

        assert adapter.counters.connected == 0, "those mids belong to a dead session"
        assert True not in connection_log
        assert adapter.counters.stale_subacks_ignored == len(old_mids)
    finally:
        await adapter.stop()


async def test_a_clean_session_after_a_failed_one_still_connects(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Failing a session must not poison the next one."""
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    try:
        transport.complete_connect()
        first = list(transport.pending_subacks)
        transport.send_suback(first[0], granted=SUBACK_FAILURE)
        assert adapter.counters.sessions_failed == 1

        transport.connect_and_subscribe()

        assert adapter.counters.connected == 1
        assert adapter.subscriptions_confirmed is True
        assert connection_log[-1] is True
    finally:
        await adapter.stop()


# -- every reconnect attempt is independently jittered ---------------------


async def test_each_reconnect_attempt_gets_its_own_jitter(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """A single seeded delay is not per-attempt jitter.

    With a scripted RNG the exact delay of every attempt is predictable, which
    is what makes "independently computed" checkable rather than asserted.
    """
    handler = RecordingHandler()
    scripted = [0.0, 1.0, 0.5, 0.25, 0.75]
    adapter = PahoMqttAdapter(
        mqtt_settings,
        handler,
        transport_factory=lambda _s: transport,
        rng=ScriptedRandom(scripted),
        suback_timeout_s=30.0,
    )

    delays = [adapter.reconnect_delay() for _ in range(len(scripted))]

    # window n is [base/2, base] with base = 1, 2, 4, 8, 16
    expected = [
        0.5 + 0.0 * 0.5,
        1.0 + 1.0 * 1.0,
        2.0 + 0.5 * 2.0,
        4.0 + 0.25 * 4.0,
        8.0 + 0.75 * 8.0,
    ]
    assert delays == pytest.approx(expected)
    # Same growth, different fraction each time: the jitter is not reused.
    assert len({round(d % 1, 6) for d in delays}) > 1


async def test_the_adapter_schedules_every_attempt_itself(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Paho's timer is parked at the cap; the adapter arms each attempt."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        scheduled_before = adapter.counters.reconnects_scheduled
        transport.drop_connection()
        await settle()

        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
        # Paho has no retry knobs to set: the transport protocol no longer
        # exposes `reconnect_delay_set`, so no competing policy can be armed.
        assert not hasattr(transport, "reconnect_delay_set")
    finally:
        await adapter.stop()


async def test_a_scheduled_attempt_actually_reconnects(
    database_url: str, transport: FakeMqttTransport
) -> None:
    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="never-logged",
    )
    handler = RecordingHandler()
    adapter = PahoMqttAdapter(
        settings,
        handler,
        transport_factory=lambda _s: transport,
        # 0.0 -> the bottom of the first window, 0.5 s.
        rng=ScriptedRandom([0.0]),
    )
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        attempts_before = transport.reconnect_calls
        transport.drop_connection()

        await asyncio.sleep(0.2)
        assert transport.reconnect_calls == attempts_before, "not before the delay"

        await asyncio.sleep(0.6)
        assert transport.reconnect_calls == attempts_before + 1
    finally:
        await adapter.stop()


async def test_a_failing_attempt_schedules_the_next_one_without_spinning(
    database_url: str, transport: FakeMqttTransport
) -> None:
    """A refused attempt must grow the delay, never retry in a tight loop."""
    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="never-logged",
    )
    handler = RecordingHandler()
    adapter = PahoMqttAdapter(
        settings,
        handler,
        transport_factory=lambda _s: transport,
        rng=ScriptedRandom([0.0, 0.0, 0.0]),
    )
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        transport.reconnect_raises = OSError("no route to host")
        attempts_before = transport.reconnect_calls
        scheduled_before = adapter.counters.reconnects_scheduled
        transport.drop_connection()

        await asyncio.sleep(0.7)  # first window is 0.5 s
        assert transport.reconnect_calls == attempts_before + 1
        assert adapter.counters.reconnect_attempt_failures == 1
        # The next attempt is armed one window further out, not immediately.
        assert adapter.counters.reconnects_scheduled == scheduled_before + 2
        assert transport.reconnect_calls == attempts_before + 1
    finally:
        await adapter.stop()


async def test_no_reconnect_is_scheduled_once_shutdown_starts(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    transport.connect_and_subscribe()
    transport.drop_connection()

    await adapter.stop()
    scheduled = adapter.counters.reconnects_scheduled
    attempts = transport.reconnect_calls

    await asyncio.sleep(0.1)

    assert adapter.counters.reconnects_scheduled == scheduled
    assert transport.reconnect_calls == attempts, "the armed timer was cancelled"


# -- exactly one reconnect owner ------------------------------------------


def test_the_real_client_has_paho_auto_reconnect_disabled(
    mqtt_settings: Settings,
) -> None:
    """The supported switch in paho-mqtt 2.x, not a very large retry delay.

    With `reconnect_on_failure` false, `loop_forever` returns when the
    connection drops instead of waiting and retrying on a schedule of its own,
    so no second reconnect policy exists to compete with the adapter's.
    """
    client = _build_paho_transport(mqtt_settings)

    assert client._reconnect_on_failure is False
    assert paho_version.startswith("2."), "the mechanism above is the 2.x one"


def test_the_adapter_never_configures_a_paho_retry_policy() -> None:
    """There is no knob to set: the transport protocol does not expose one."""
    assert not hasattr(MqttTransport, "reconnect_delay_set")
    assert not hasattr(FakeMqttTransport, "reconnect_delay_set")


async def test_an_unexpected_disconnect_has_exactly_one_scheduler(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """The adapter schedules; nothing else reconnects behind its back."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        scheduled_before = adapter.counters.reconnects_scheduled
        attempts_before = transport.reconnect_calls

        transport.drop_connection()
        await settle()

        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
        # Armed, not yet attempted: the delay is the adapter's to decide.
        assert transport.reconnect_calls == attempts_before
        assert pending_delay(adapter) > 0.0

        # Nothing else fires an attempt while that timer is waiting.
        await asyncio.sleep(0.2)
        assert transport.reconnect_calls == attempts_before
    finally:
        await adapter.stop()


# -- serialization ---------------------------------------------------------


async def test_a_burst_of_failure_signals_produces_one_attempt(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario 1: several signals before the retry fires."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        scheduled_before = adapter.counters.reconnects_scheduled
        attempts_before = transport.reconnect_calls
        counted_before = adapter.counters.reconnect_attempts

        for _ in range(5):
            transport.drop_connection()
        adapter.request_reconnect("and an explicit one too")
        await settle()

        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
        assert adapter.counters.reconnects_coalesced == 5

        await asyncio.sleep(1.2)
        assert transport.reconnect_calls == attempts_before + 1
        assert adapter.counters.reconnect_attempts == counted_before + 1
    finally:
        await adapter.stop()


async def test_a_trigger_during_an_attempt_does_not_overlap_it(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario 2: a retry trigger while the attempt is still running."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        transport.connect_and_subscribe()
        transport.reconnect_entered.clear()
        transport.reconnect_gate = gate
        attempts_before = transport.reconnect_calls

        transport.drop_connection()
        await wait_until(lambda: transport.reconnect_calls > attempts_before)

        # The attempt is blocked inside the transport. Trigger more failures.
        coalesced_before = adapter.counters.reconnects_coalesced
        for _ in range(3):
            transport.drop_connection()
        await settle()

        assert transport.reconnect_calls == attempts_before + 1, "no second attempt"
        assert adapter.counters.reconnects_coalesced == coalesced_before + 3
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_the_event_loop_keeps_running_while_reconnect_blocks(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario 6: the synchronous call must not be on the loop.

    While the transport's `reconnect()` is blocked, the loop still handles an
    inbound delivery end to end — validation, decision and acknowledgement.
    """
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        transport.connect_and_subscribe()
        transport.reconnect_entered.clear()
        transport.reconnect_gate = gate
        attempts_before = transport.reconnect_calls

        transport.drop_connection()
        await wait_until(lambda: transport.reconnect_calls > attempts_before)

        # The worker thread is parked inside reconnect(); the loop is not.
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        assert ticks == 5

        mid = transport.deliver(telemetry_topic(DEVICE_ID), PAYLOAD)
        await settle()
        assert transport.acks == [(mid, 1)], "ingestion ran while reconnect blocked"
        assert handler.seen[0].mid == mid
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_a_failed_attempt_schedules_the_next_independently_jittered(
    database_url: str, transport: FakeMqttTransport
) -> None:
    """Scenario 3: failure schedules, never retries inline."""
    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="never-logged",
    )
    handler = RecordingHandler()
    adapter = PahoMqttAdapter(
        settings,
        handler,
        transport_factory=lambda _s: transport,
        rng=ScriptedRandom([0.0, 1.0, 0.0]),
    )
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        counted_before = adapter.counters.reconnect_attempts
        transport.reconnect_raises = OSError("no route to host")
        transport.drop_connection()

        # First window is [0.5, 1.0]; with random()==0.0 it is exactly 0.5.
        await wait_until(lambda: adapter.counters.reconnect_attempt_failures == 1)
        await settle()

        # The next attempt is armed with its own fresh jitter, not retried now.
        delay = pending_delay(adapter)
        assert 1.0 <= delay <= 2.0, "second window, jittered independently"
        assert adapter.counters.reconnect_attempts == counted_before + 1
    finally:
        transport.reconnect_raises = None
        await adapter.stop()


# -- shutdown --------------------------------------------------------------


async def test_shutdown_while_waiting_for_a_retry_never_starts_it(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario 4."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    transport.connect_and_subscribe()
    transport.drop_connection()
    await settle()
    attempts_before = transport.reconnect_calls
    counted_before = adapter.counters.reconnect_attempts
    assert pending_delay(adapter) > 0.0

    await adapter.stop()
    await asyncio.sleep(0.3)

    assert transport.reconnect_calls == attempts_before
    assert adapter.counters.reconnect_attempts == counted_before


async def test_a_reconnect_finishing_after_shutdown_cannot_revive_the_session(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario 5: the attempt completes, but the process is already done."""
    handler = RecordingHandler()
    connection_log: list[bool] = []
    adapter = build_adapter(mqtt_settings, handler, transport, connection_log)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    transport.connect_and_subscribe()
    transport.reconnect_entered.clear()
    transport.reconnect_gate = gate
    attempts_before = transport.reconnect_calls

    transport.drop_connection()
    await wait_until(lambda: transport.reconnect_calls > attempts_before)

    # Shutdown starts while the attempt is parked inside the transport.
    stopping = asyncio.ensure_future(adapter.stop())
    await settle()
    gate.set()
    await stopping

    assert adapter.counters.reconnects_discarded_after_shutdown == 1
    assert connection_log[-1] is False
    # The session was torn down again rather than left running.
    assert transport.disconnects >= 1
    assert transport.loop_stopped >= 1

    # A late CONNACK from that attempt changes nothing either.
    transport.complete_connect()
    assert adapter.counters.connected == 1, "the pre-shutdown session only"
    transport.reconnect_gate = None


# -- a failure arriving during an attempt is not lost ----------------------
#
# The race: the attempt in flight may have been started before the failure
# happened, so it can return "successfully" on a session that signal has
# already invalidated. Coalescing without remembering left nothing pending and
# the adapter never retried.


async def blocked_attempt(
    adapter: PahoMqttAdapter, transport: FakeMqttTransport, gate: threading.Event
) -> int:
    """Drive the adapter into an in-flight, blocked reconnect attempt."""
    transport.connect_and_subscribe()
    transport.reconnect_entered.clear()
    transport.reconnect_gate = gate
    attempts_before = transport.reconnect_calls
    transport.drop_connection()
    await wait_until(lambda: transport.reconnect_calls > attempts_before)
    assert adapter._reconnect_task is not None
    return attempts_before


async def test_a_failure_during_an_attempt_is_remembered_and_retried(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario A."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        await blocked_attempt(adapter, transport, gate)
        scheduled_before = adapter.counters.reconnects_scheduled

        # A definitive failure lands while the attempt is still running.
        transport.drop_connection()
        await settle()
        assert adapter.counters.failures_deferred == 1
        assert adapter._deferred_failure is not None

        # The attempt finishes without the session ever becoming usable.
        transport.reconnect_gate = None
        gate.set()
        await wait_until(lambda: adapter._reconnect_task is None)
        await settle()

        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
        assert adapter._deferred_failure is None
        assert pending_delay(adapter) > 0.0
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_a_session_that_becomes_healthy_cancels_the_remembered_failure(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario B: the replacement session works, so there is nothing to retry."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        await blocked_attempt(adapter, transport, gate)

        transport.drop_connection()
        await settle()
        assert adapter.counters.failures_deferred == 1

        # The broker answers and the new session is confirmed before the
        # attempt's completion handling runs.
        transport.connect_and_subscribe()
        assert adapter.subscriptions_confirmed is True
        assert adapter._deferred_failure is None

        scheduled_before = adapter.counters.reconnects_scheduled
        transport.reconnect_gate = None
        gate.set()
        await wait_until(lambda: adapter._reconnect_task is None)
        await settle()

        assert adapter.counters.reconnects_scheduled == scheduled_before
        assert adapter._reconnect_timer is None
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_shutdown_beats_a_remembered_failure(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario C."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()

    await blocked_attempt(adapter, transport, gate)
    transport.drop_connection()
    await settle()
    assert adapter.counters.failures_deferred == 1
    scheduled_before = adapter.counters.reconnects_scheduled

    stopping = asyncio.ensure_future(adapter.stop())
    await settle()
    transport.reconnect_gate = None
    gate.set()
    await stopping
    await asyncio.sleep(0.2)

    assert adapter.counters.reconnects_scheduled == scheduled_before
    assert adapter._reconnect_timer is None
    assert adapter._deferred_failure is None


async def test_many_failures_during_one_attempt_become_one_retry(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Scenario D."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        attempts_before = await blocked_attempt(adapter, transport, gate)
        scheduled_before = adapter.counters.reconnects_scheduled

        for _ in range(4):
            transport.drop_connection()
        adapter.request_reconnect("and an explicit one")
        adapter._fail_session("and a refused subscription")
        await settle()

        assert adapter.counters.failures_deferred == 6
        assert transport.reconnect_calls == attempts_before + 1, "no overlap"

        transport.reconnect_gate = None
        gate.set()
        await wait_until(lambda: adapter._reconnect_task is None)
        await settle()

        # Six signals, one retry.
        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_the_resumed_retry_uses_a_fresh_jitter_window(
    database_url: str, transport: FakeMqttTransport
) -> None:
    """Scenario E: the deferred failure is retried under the normal policy."""
    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="never-logged",
    )
    handler = RecordingHandler()
    adapter = PahoMqttAdapter(
        settings,
        handler,
        transport_factory=lambda _s: transport,
        rng=ScriptedRandom([0.0, 1.0, 0.0]),
    )
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        await blocked_attempt(adapter, transport, gate)
        transport.drop_connection()
        await settle()

        transport.reconnect_gate = None
        gate.set()
        await wait_until(lambda: adapter._reconnect_task is None)
        await settle()

        # Third draw from the scripted RNG; the window has grown to [2, 4].
        delay = pending_delay(adapter)
        assert 2.0 <= delay <= 4.0
        assert delay <= RECONNECT_MAX_S
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_a_failure_while_a_retry_is_merely_armed_is_still_coalesced(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Nothing is lost there, so it must not become a second pending retry."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        transport.drop_connection()
        await settle()
        assert pending_delay(adapter) > 0.0
        deferred_before = adapter.counters.failures_deferred

        transport.drop_connection()
        await settle()

        # A retry is already armed: coalesced, and nothing is remembered.
        assert adapter.counters.failures_deferred == deferred_before
        assert adapter._deferred_failure is None
    finally:
        await adapter.stop()


# -- a healthy session supersedes the retry its failure armed --------------
#
# The deferred failure arms a retry after the attempt completes. If the
# replacement session is then confirmed before that timer fires, firing it
# would drop a working connection for a failure it has already recovered from.


async def armed_deferred_retry(
    adapter: PahoMqttAdapter, transport: FakeMqttTransport, gate: threading.Event
) -> None:
    """Walk steps 1-5 of the ordering: end with a deferred retry armed."""
    transport.connect_and_subscribe()  # 1: a session to lose
    transport.reconnect_entered.clear()
    transport.reconnect_gate = gate
    attempts_before = transport.reconnect_calls
    transport.drop_connection()
    await wait_until(lambda: transport.reconnect_calls > attempts_before)  # attempt in flight

    transport.drop_connection()  # 2: a definitive failure during the attempt
    await settle()
    assert adapter._deferred_failure is not None  # 3: stored

    transport.reconnect_gate = None
    gate.set()  # 4: the attempt completes
    await wait_until(lambda: adapter._reconnect_task is None)
    await settle()
    assert adapter._reconnect_timer is not None, "5: the deferred retry is armed"


async def test_a_confirmed_session_cancels_the_deferred_retry(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """The exact ordering in the finding, steps 1-10."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        await armed_deferred_retry(adapter, transport, gate)
        deadline = pending_delay(adapter)
        assert deadline > 0.0
        attempts_before = transport.reconnect_calls
        scheduled_before = adapter.counters.reconnects_scheduled

        # 6 and 7: the replacement session is confirmed before the timer fires.
        transport.connect_and_subscribe()
        await settle()
        assert adapter.subscriptions_confirmed is True

        # 8: the retry is superseded.
        assert adapter.counters.retries_superseded == 1
        assert adapter._reconnect_timer is None
        assert adapter._deferred_failure is None

        # 9 and 10: nothing reconnects after the original deadline passes.
        await asyncio.sleep(deadline + 0.3)
        assert transport.reconnect_calls == attempts_before
        assert adapter.counters.reconnects_scheduled == scheduled_before
        assert adapter.subscriptions_confirmed is True
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_a_new_failure_after_the_confirmation_still_reconnects(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Case A: superseding one retry must not disable retrying."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()
    try:
        await armed_deferred_retry(adapter, transport, gate)
        transport.connect_and_subscribe()
        await settle()
        assert adapter._reconnect_timer is None
        scheduled_before = adapter.counters.reconnects_scheduled

        # A genuinely new failure on the healthy session.
        transport.drop_connection()
        await settle()

        assert adapter.counters.reconnects_scheduled == scheduled_before + 1
        assert pending_delay(adapter) > 0.0
        assert adapter.subscriptions_confirmed is False
    finally:
        gate.set()
        transport.reconnect_gate = None
        await adapter.stop()


async def test_a_stale_confirmation_cannot_cancel_the_current_retry(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Case B: a confirmation for a superseded session has no say."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        transport.drop_connection()
        await settle()
        assert adapter._reconnect_timer is not None
        current = adapter._generation
        # The drop already made the earlier confirmation stale; count from here.
        stale_before = adapter.counters.stale_health_ignored

        # A confirmation belonging to a session two generations old.
        adapter._supersede_retry_for(current - 2)

        assert adapter.counters.stale_health_ignored == stale_before + 1
        assert adapter.counters.retries_superseded == 0
        assert adapter._reconnect_timer is not None, "the current retry stands"
    finally:
        await adapter.stop()


async def test_a_confirmation_does_not_cancel_a_newer_sessions_retry(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Case B, the other way round: the armed retry belongs to a later session."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    try:
        transport.connect_and_subscribe()
        transport.drop_connection()
        await settle()
        armed_for = adapter._armed_generation
        assert armed_for is not None

        # Pretend the confirmation came from before that retry was armed, while
        # the generation counter still matches the current session.
        adapter._session_healthy = True
        adapter._armed_generation = armed_for + 1
        adapter._supersede_retry_for(adapter._generation)

        assert adapter.counters.retries_superseded == 0
        assert adapter._reconnect_timer is not None
    finally:
        adapter._session_healthy = False
        await adapter.stop()


async def test_shutdown_after_a_confirmation_never_reconnects(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Case C."""
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    await connected(transport)
    gate = threading.Event()

    await armed_deferred_retry(adapter, transport, gate)
    transport.connect_and_subscribe()
    await settle()
    attempts_before = transport.reconnect_calls
    scheduled_before = adapter.counters.reconnects_scheduled

    await adapter.stop()
    await asyncio.sleep(0.3)

    assert transport.reconnect_calls == attempts_before
    assert adapter.counters.reconnects_scheduled == scheduled_before
    assert adapter._reconnect_timer is None


async def test_the_first_connect_is_never_superseded(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    """Only a retry armed for a failure may be cancelled by a confirmation.

    Cancelling the initial connect would leave nothing bringing the transport
    up at all.
    """
    handler = RecordingHandler()
    adapter = build_adapter(mqtt_settings, handler, transport)
    await adapter.start()
    try:
        # The first connect is armed but has not run yet.
        assert adapter._armed_is_retry is False

        transport.connect_and_subscribe()
        await settle()

        assert adapter.counters.retries_superseded == 0
        await connected(transport)
        assert transport.loop_started == 1
    finally:
        await adapter.stop()
