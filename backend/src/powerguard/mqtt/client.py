"""Paho MQTT 3.1.1 adapter.

Paho owns its own network thread. Its callback does the least possible work:
stamp ``received_at``, build an immutable record and hand it to the application
loop through a bounded queue. All domain work happens on the loop.

The adapter never talks to ``paho`` directly through a module-level import: it
is handed a :class:`MqttTransport`, which is the exact slice of the Paho client
it uses. Tests inject a fake transport and therefore exercise *this* code —
queueing, subscription confirmation, acknowledgement, saturation, redelivery
and shutdown — rather than a reimplementation of it.

Three rules shape the connection state machine:

* a session is only usable once the **broker** has confirmed *every* required
  subscription with a SUBACK granting at least QoS 1. ``subscribe()`` returning
  success only means the packet was queued locally;
* a subscription session fails as a whole. One refused filter invalidates the
  session immediately, and every SUBACK belonging to it — including ones still
  in flight — is ignored from that moment. Sessions therefore carry a
  generation, because brokers reuse message ids;
* if the queue is full the delivery is *not* acknowledged and the session is
  dropped so the broker redelivers. Silently discarding an acknowledged message
  is never acceptable.

Reconnection has exactly one owner: this adapter. Paho's automatic reconnect is
switched off at construction with ``reconnect_on_failure=False``, the supported
mechanism in paho-mqtt 2.x, so its network loop never schedules an attempt of
its own and there is no second policy to reason about. Its retry timing knobs
are not used at all.

Every attempt therefore runs through :meth:`PahoMqttAdapter._schedule_reconnect`,
which picks a fresh jittered delay and arms it on the application loop, and
through :meth:`PahoMqttAdapter._run_reconnect`, which performs the blocking
``reconnect()`` in a worker thread so the loop stays responsive. At most one
attempt exists at a time: while a timer is armed or an attempt is in flight,
further failure signals are coalesced rather than stacked. A failure that
arrives *during* an attempt is remembered rather than dropped, because the
attempt in flight may have been started before that failure happened and may
finish believing all is well — leaving nothing pending.

A confirmed session is authoritative in the other direction: once every
subscription is acknowledged, any retry still armed for that session or an older
one is superseded, so a working connection is never dropped to satisfy a
failure it has already recovered from. Armed retries carry the generation they
belong to, so a late confirmation cannot cancel a retry that belongs to a newer,
still-failed session.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from powerguard.config import Settings
from powerguard.mqtt.ports import AckDecision, InboundMessage
from powerguard.mqtt.topics import SUBSCRIPTIONS
from powerguard.observability import log_event

logger = logging.getLogger(__name__)

Handler = Callable[[InboundMessage], Awaitable[AckDecision]]

# Capped exponential reconnect, matching the firmware policy in spirit: a
# broker restart must not be met by every backend instance at the same instant.
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 60.0
_MAX_ATTEMPT_SHIFT = 6  # 1, 2, 4, 8, 16, 32, then capped at RECONNECT_MAX_S

# A broker that accepts CONNECT but never answers SUBSCRIBE would otherwise
# leave the adapter permanently "connecting" and permanently deaf.
SUBACK_TIMEOUT_S = 10.0

# MQTT_SPEC requires QoS 1: a SUBACK granting QoS 0 removes redelivery, which
# the whole acknowledgement design depends on.
MINIMUM_GRANTED_QOS = 1

MQTT_ERR_SUCCESS = 0



class MqttTransport(Protocol):
    """The slice of ``paho.mqtt.client.Client`` this adapter depends on."""

    on_connect: Any
    on_disconnect: Any
    on_message: Any
    on_subscribe: Any

    def manual_ack_set(self, on: bool) -> None: ...

    def username_pw_set(self, username: str, password: str | None = None) -> None: ...

    def connect_async(self, host: str, port: int, keepalive: int) -> None: ...

    def loop_start(self) -> None: ...

    def loop_stop(self) -> None: ...

    def disconnect(self) -> None: ...

    def reconnect(self) -> int: ...

    def subscribe(self, topic: str, qos: int) -> tuple[int, int | None]: ...

    def unsubscribe(self, topic: str) -> tuple[int, int | None]: ...

    def ack(self, mid: int, qos: int) -> None: ...


TransportFactory = Callable[[Settings], MqttTransport]


@dataclass(slots=True)
class MqttCounters:
    """Process-local adapter diagnostics. No metrics endpoint in this phase."""

    connected: int = 0
    connect_failures: int = 0
    disconnected: int = 0
    unexpected_disconnects: int = 0
    subscribed: int = 0
    subscribe_failures: int = 0
    subscriptions_refused: int = 0
    suback_timeouts: int = 0
    received: int = 0
    enqueued: int = 0
    saturated: int = 0
    dropped_after_shutdown: int = 0
    acknowledged: int = 0
    withheld: int = 0
    handler_errors: int = 0
    sessions_failed: int = 0
    stale_subacks_ignored: int = 0
    reconnects_scheduled: int = 0
    reconnects_requested: int = 0
    reconnects_coalesced: int = 0
    failures_deferred: int = 0
    retries_superseded: int = 0
    stale_health_ignored: int = 0
    reconnect_attempts: int = 0
    reconnect_attempt_failures: int = 0
    reconnects_discarded_after_shutdown: int = 0
    drain_timeouts: int = 0


@dataclass(frozen=True, slots=True)
class _PendingSubscription:
    """A SUBSCRIBE awaiting its SUBACK, tagged with the session that sent it.

    The generation matters: MQTT message ids are 16-bit and brokers reuse them,
    so a mid alone cannot tell a live SUBACK from one belonging to a session
    that has already failed.
    """

    generation: int
    topic: str


def reason_is_failure(reason: object) -> bool:
    """True when a Paho CONNACK reason means the session was refused.

    Paho 2 passes a ``ReasonCode``; older callbacks pass a plain int. An
    unrecognised object is treated as a failure, because marking the adapter
    connected on a value we cannot read would report health we cannot prove.
    """
    if reason is None:
        return True
    is_failure = getattr(reason, "is_failure", None)
    if isinstance(is_failure, bool):
        return bool(is_failure)
    try:
        return bool(int(reason) != 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return True


def granted_qos(code: object) -> int | None:
    """Granted QoS from one SUBACK entry, or None when the filter was refused.

    MQTT 3.1.1 encodes a refusal as 0x80. Paho 2 wraps each entry in a
    ``ReasonCode`` whose ``is_failure`` says the same thing, while its numeric
    value *is* the granted QoS — so a SUBACK entry must never be read with
    :func:`reason_is_failure`, where any non-zero value means failure.
    """
    if code is None:
        return None
    is_failure = getattr(code, "is_failure", None)
    if isinstance(is_failure, bool) and is_failure:
        return None
    value = getattr(code, "value", code)
    try:
        numeric = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    numeric = int(numeric)
    if numeric not in (0, 1, 2):
        return None
    return numeric


def _build_paho_transport(settings: Settings) -> MqttTransport:
    """Default transport: a real Paho client configured for QoS 1 manual ack."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"powerguard-backend-{settings.mqtt_instance_id}",
        protocol=mqtt.MQTTv311,
        clean_session=False,  # persistent session: QoS 1 survives a restart
        # The supported way to switch off Paho's own reconnect loop in 2.x.
        # With it false, `loop_forever` returns when the connection drops
        # instead of waiting and retrying on its own schedule, leaving this
        # adapter as the only component that decides when to reconnect.
        reconnect_on_failure=False,
    )
    if settings.mqtt_username and settings.mqtt_password:
        client.username_pw_set(
            settings.mqtt_username, settings.mqtt_password.get_secret_value()
        )
    transport: MqttTransport = client
    return transport


class PahoMqttAdapter:
    def __init__(
        self,
        settings: Settings,
        handler: Handler,
        *,
        on_connection_change: Callable[[bool], None] | None = None,
        transport_factory: TransportFactory | None = None,
        rng: random.Random | None = None,
        suback_timeout_s: float = SUBACK_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._on_connection_change = on_connection_change or (lambda _connected: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[InboundMessage] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        # One redelivery request per session: a burst of withheld messages must
        # not turn into a reconnect storm.
        self._redelivery_requested = False
        self._reconnect_attempt = 0
        self._rng = rng or random.Random()
        self._suback_timeout_s = suback_timeout_s
        # Subscription mids the broker has not answered yet, per session.
        self._pending_subacks: dict[int, _PendingSubscription] = {}
        # Bumped on every CONNACK and on every session failure, so SUBACKs from
        # a dead session can never revive it.
        self._generation = 0
        self._suback_timer: asyncio.TimerHandle | None = None
        self._reconnect_timer: asyncio.TimerHandle | None = None
        # At most one attempt in flight, ever.
        self._reconnect_task: asyncio.Task[None] | None = None
        self._pending_reason = "initial connect"
        # A definitive failure that arrived while an attempt was running, kept
        # until that attempt finishes so the signal cannot be lost.
        self._deferred_failure: str | None = None
        # Whether the session is currently usable, as last reported.
        self._session_healthy = False
        # The session generation an armed retry belongs to, so a confirmation
        # can tell "this retry is mine to cancel" from "this one is newer".
        self._armed_generation: int | None = None
        # Whether what is armed is a retry for a failure, as opposed to the
        # first connect. Only the former can be superseded by a healthy
        # session; cancelling the former would leave nothing connecting at all.
        self._armed_is_retry = False
        self.counters = MqttCounters()

        factory = transport_factory or _build_paho_transport
        self._client = factory(settings)
        # Manual acknowledgement is the adapter's own contract, not a property
        # of whichever client it was handed: a delivery is confirmed only once
        # the backend has durably decided what to do with it. Enforced here so
        # an injected transport cannot silently auto-acknowledge.
        self._client.manual_ack_set(True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

    # -- lifecycle ---------------------------------------------------------

    @property
    def subscriptions_confirmed(self) -> bool:
        """True while this session is usable: confirmed and not since lost."""
        return self._session_healthy and not self._pending_subacks

    def _report_connection(self, connected: bool) -> None:
        """The one place the adapter's health is set and published.

        Becoming healthy also clears a deferred failure: it described a session
        that has since been replaced by a working one.
        """
        self._session_healthy = connected
        if connected:
            self._deferred_failure = None
        self._on_connection_change(connected)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._settings.mqtt_ingress_queue_size)
        self._worker = asyncio.create_task(self._drain())
        # connect_async only records the destination: it performs no I/O and
        # starts no thread. The connection itself is made by the scheduler,
        # like every later attempt, so the first connect is not a special case
        # driven by Paho's own retry-the-first-connection loop.
        self._client.connect_async(
            self._settings.mqtt_host,
            self._settings.mqtt_port,
            self._settings.mqtt_keepalive_s,
        )
        log_event(
            logger,
            logging.INFO,
            "mqtt_starting",
            host=self._settings.mqtt_host,
            port=self._settings.mqtt_port,
        )
        self._schedule_reconnect("initial connect", immediate=True)

    async def stop(self) -> None:
        """Stop intake, drain what was accepted, then leave the session cleanly.

        The order is fixed and must stay that way:

        1. refuse new intake — nothing arriving from here on is enqueued, and
           nothing is acknowledged that was not handled, so the broker keeps it;
        2. unsubscribe, so the broker stops sending;
        3. drain accepted work **while the network thread still runs**, or the
           acknowledgements produced during the drain would never be sent and
           every drained message would be redelivered;
        4. disconnect, then stop the loop, then retire the worker.
        """
        self._stopping = True
        self._disarm_suback_timer()
        self._cancel_reconnect_timer()
        self._deferred_failure = None
        self._pending_subacks.clear()
        # An attempt already in flight is given a moment to finish so its own
        # shutdown check can tear the session down; it is never left to race.
        attempt = self._reconnect_task
        if attempt is not None:
            await asyncio.wait({attempt}, timeout=self._settings.mqtt_shutdown_grace_s)

        for topic_filter in SUBSCRIPTIONS:
            with contextlib.suppress(Exception):
                self._client.unsubscribe(topic_filter)

        if self._worker is not None:
            try:
                await asyncio.wait_for(
                    self._drain_remaining(), timeout=self._settings.mqtt_shutdown_grace_s
                )
            except TimeoutError:
                self.counters.drain_timeouts += 1
                log_event(logger, logging.WARNING, "mqtt_shutdown_drain_timeout")

        self._client.disconnect()
        self._client.loop_stop()

        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        self._report_connection(False)
        log_event(logger, logging.INFO, "mqtt_stopped")

    async def _drain_remaining(self) -> None:
        if self._queue is None:
            return
        await self._queue.join()

    # -- SUBACK tracking ---------------------------------------------------

    def _from_paho_thread(self, callback: Callable[[], None]) -> None:
        """Run a loop-owned action from Paho's network thread."""
        loop = self._loop
        if loop is None:  # pragma: no cover - before start()
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(callback)

    def _arm_suback_timer(self) -> None:
        """Called on the application loop."""
        self._disarm_suback_timer()
        if self._stopping or not self._pending_subacks:
            return
        loop = self._loop
        if loop is None:  # pragma: no cover - before start()
            return
        self._suback_timer = loop.call_later(self._suback_timeout_s, self._on_suback_timeout)

    def _disarm_suback_timer(self) -> None:
        if self._suback_timer is not None:
            self._suback_timer.cancel()
            self._suback_timer = None

    def _on_suback_timeout(self) -> None:
        self._suback_timer = None
        if self._stopping or not self._pending_subacks:
            return
        outstanding = len(self._pending_subacks)
        self.counters.suback_timeouts += 1
        log_event(
            logger,
            logging.ERROR,
            "mqtt_suback_timeout",
            pending=outstanding,
            timeout_s=self._suback_timeout_s,
        )
        # Connected but unconfirmed is the same as deaf: drop and retry.
        self._fail_session("suback timeout")

    def _fail_session(self, reason: str) -> None:
        """Invalidate the whole subscription session and start a new one.

        A session is all-or-nothing. One refused filter makes the session
        useless, so the remaining pending SUBACKs are abandoned together and the
        generation is bumped: any SUBACK still in flight for this session — even
        an accepting one for a filter that had not answered yet — can no longer
        mark the adapter connected.
        """
        self._pending_subacks.clear()
        self._generation += 1
        self.counters.sessions_failed += 1
        self._disarm_suback_timer()
        self._report_connection(False)
        log_event(
            logger,
            logging.ERROR,
            "mqtt_session_failed",
            reason=reason,
            generation=self._generation,
        )
        self.request_reconnect(reason)

    def _on_subscribe(
        self,
        _client: MqttTransport,
        _userdata: object,
        mid: int,
        reason_codes: object,
        _props: object = None,
    ) -> None:
        pending = self._pending_subacks.get(mid)
        if pending is None or pending.generation != self._generation:
            # A SUBACK for a session that has already failed, timed out or been
            # replaced. Mids are reused, so the generation is what decides.
            self.counters.stale_subacks_ignored += 1
            log_event(
                logger,
                logging.INFO,
                "mqtt_stale_suback_ignored",
                mid=mid,
                generation=self._generation,
            )
            return

        codes = list(reason_codes) if isinstance(reason_codes, list | tuple) else [reason_codes]
        qos_values = [granted_qos(code) for code in codes]
        refused = [value for value in qos_values if value is None or value < MINIMUM_GRANTED_QOS]
        if not codes or refused:
            self.counters.subscriptions_refused += 1
            log_event(
                logger,
                logging.ERROR,
                "mqtt_subscription_refused",
                topic=pending.topic,
                granted=str(qos_values),
            )
            # The session dies as a whole, with every other pending mid.
            self._fail_session("subscription refused by broker")
            return

        topic_filter = pending.topic
        del self._pending_subacks[mid]
        self.counters.subscribed += 1
        log_event(
            logger,
            logging.INFO,
            "mqtt_subscribed",
            topic=topic_filter,
            granted_qos=qos_values[0],
        )
        if self._pending_subacks:
            return

        # Every filter confirmed: only now is the session usable.
        confirmed_generation = pending.generation
        self._from_paho_thread(self._disarm_suback_timer)
        self.counters.connected += 1
        self._reconnect_attempt = 0
        self._redelivery_requested = False
        self._report_connection(True)
        log_event(logger, logging.INFO, "mqtt_connected", subscriptions=len(SUBSCRIPTIONS))
        # The timer is loop-owned, so the supersede runs there.
        self._from_paho_thread(lambda: self._supersede_retry_for(confirmed_generation))

    def _supersede_retry_for(self, generation: int) -> None:
        """A confirmed session cancels the retry that its failure had armed.

        Runs on the application loop. Without this a session that recovered
        while the retry was still waiting would be dropped again when the timer
        fired, for a failure it had already recovered from.
        """
        if generation != self._generation:
            # A late confirmation for a session that has since been replaced:
            # it has no say over the retry the current one is waiting on.
            self.counters.stale_health_ignored += 1
            log_event(
                logger,
                logging.INFO,
                "mqtt_stale_health_ignored",
                generation=generation,
                current=self._generation,
            )
            return
        if not self._session_healthy:
            # A newer failure has already landed; its retry stands.
            return
        self._deferred_failure = None
        if self._reconnect_timer is None:
            return
        if not self._armed_is_retry:
            # The first connect is still what brings the transport up.
            return
        if self._armed_generation is not None and self._armed_generation > generation:
            # Armed for a newer session than the one just confirmed.
            return
        self.counters.retries_superseded += 1
        log_event(
            logger,
            logging.INFO,
            "mqtt_retry_superseded_by_healthy_session",
            generation=generation,
        )
        self._cancel_reconnect_timer()

    # -- paho thread -------------------------------------------------------

    def _on_connect(
        self,
        client: MqttTransport,
        _userdata: object,
        _flags: object,
        reason: object,
        _props: object = None,
    ) -> None:
        self._pending_subacks.clear()
        self._generation += 1
        generation = self._generation
        if reason_is_failure(reason):
            # A refused CONNACK is not a connection: never report it as one.
            self.counters.connect_failures += 1
            self._report_connection(False)
            log_event(logger, logging.ERROR, "mqtt_connect_refused", reason=str(reason))
            self._schedule_reconnect("connack refused")
            return
        if self._stopping:  # pragma: no cover - CONNACK racing shutdown
            return

        for topic_filter in SUBSCRIPTIONS:
            result, mid = client.subscribe(topic_filter, 1)
            if result != MQTT_ERR_SUCCESS or mid is None:
                # The SUBSCRIBE packet was not even queued locally.
                self.counters.subscribe_failures += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "mqtt_subscribe_failed",
                    topic=topic_filter,
                    result=result,
                )
                self._fail_session("subscribe rejected locally")
                return
            self._pending_subacks[mid] = _PendingSubscription(generation, topic_filter)

        # Still not connected: the broker has confirmed nothing yet.
        log_event(
            logger,
            logging.INFO,
            "mqtt_subscribe_sent",
            reason=str(reason),
            pending=len(self._pending_subacks),
        )
        self._from_paho_thread(self._arm_suback_timer)

    def _on_disconnect(self, _client: MqttTransport, _userdata: object, *args: object) -> None:
        self.counters.disconnected += 1
        # The session is over: its pending SUBACKs are void and a later one must
        # not be able to mark the adapter connected.
        self._pending_subacks.clear()
        self._generation += 1
        self._from_paho_thread(self._disarm_suback_timer)
        self._report_connection(False)
        if self._stopping:
            log_event(logger, logging.INFO, "mqtt_disconnected", expected=True)
            return
        self.counters.unexpected_disconnects += 1
        log_event(logger, logging.WARNING, "mqtt_disconnected", expected=False)
        # Paho retries on its own; without re-seeding the delay it would do so
        # on a plain exponential with no jitter, outside the approved policy.
        self._schedule_reconnect("unexpected disconnect")

    def _on_message(self, _client: MqttTransport, _userdata: object, message: Any) -> None:
        # Server receive time is captured here, at the edge (ADR-006).
        received_at = dt.datetime.now(dt.UTC)
        self.counters.received += 1
        if self._stopping:
            # Shutdown has begun: nothing new is accepted. It is not
            # acknowledged either, so the broker redelivers it.
            self.counters.dropped_after_shutdown += 1
            log_event(
                logger, logging.INFO, "mqtt_message_dropped_after_shutdown", topic=message.topic
            )
            return
        if self._loop is None or self._queue is None:  # pragma: no cover - before start()
            return
        record = InboundMessage(
            topic=message.topic,
            payload=bytes(message.payload),
            received_at=received_at,
            mid=message.mid,
            qos=message.qos,
            retained=bool(message.retain),
        )
        self._from_paho_thread(lambda: self._enqueue(record))

    def _enqueue(self, record: InboundMessage) -> None:
        assert self._queue is not None
        if self._stopping:
            # Shutdown began between the network callback and this loop tick.
            self.counters.dropped_after_shutdown += 1
            return
        try:
            self._queue.put_nowait(record)
            self.counters.enqueued += 1
        except asyncio.QueueFull:
            # Never acknowledged, so the broker will redeliver after reconnect.
            self.counters.saturated += 1
            log_event(
                logger,
                logging.ERROR,
                "mqtt_ingress_saturated",
                queue_size=self._queue.maxsize,
            )
            self.request_reconnect("ingress queue saturated")

    # -- application loop --------------------------------------------------

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            record = await self._queue.get()
            try:
                decision = await self._handler(record)
                if decision is AckDecision.ACK:
                    self.acknowledge(record)
                    self.counters.acknowledged += 1
                else:
                    self._withhold("withheld acknowledgement")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.counters.handler_errors += 1
                logger.exception("ingest_failed topic=%s", record.topic)
                # An unexpected failure was not a durable decision either.
                self._withhold("ingest handler failed")
            finally:
                self._queue.task_done()

    def _withhold(self, reason: str) -> None:
        """Record a withheld acknowledgement and arrange redelivery.

        A QoS 1 delivery that is never acknowledged stays unacknowledged for the
        whole session: the broker only re-sends it after the session drops. So a
        withheld message must actively trigger that drop, once per session.
        """
        self.counters.withheld += 1
        if self._stopping or self._redelivery_requested:
            return
        self._redelivery_requested = True
        self.request_reconnect(reason)

    # -- port --------------------------------------------------------------

    def acknowledge(self, message: InboundMessage) -> None:
        self._client.ack(message.mid, message.qos)

    def reconnect_delay(self) -> float:
        """Capped exponential delay with jitter, in seconds.

        Jitter spreads reconnects: several backends restarted together must not
        retry in lockstep.
        """
        self._reconnect_attempt = min(self._reconnect_attempt + 1, _MAX_ATTEMPT_SHIFT + 1)
        base = min(RECONNECT_BASE_S * (2 ** (self._reconnect_attempt - 1)), RECONNECT_MAX_S)
        # Full jitter over the upper half of the window: never below 0.5 * base.
        return float(base / 2.0 + self._rng.random() * (base / 2.0))

    def _schedule_reconnect(self, reason: str, *, immediate: bool = False) -> None:
        """Ask for a connection attempt. Callable from either thread.

        The decision itself is made on the application loop, which owns the
        timer and the in-flight task, so two threads can never arm two attempts.
        """
        if self._stopping:
            return
        self._from_paho_thread(lambda: self._schedule_on_loop(reason, immediate))

    def _schedule_on_loop(self, reason: str, immediate: bool) -> None:
        loop = self._loop
        if self._stopping or loop is None:
            return
        if self._reconnect_task is not None:
            # An attempt is running. It may have started before this failure
            # happened, so it could finish "successfully" on a session this
            # signal has already invalidated. Remember the signal instead of
            # dropping it: `_run_reconnect` acts on it once the attempt ends.
            self.counters.reconnects_coalesced += 1
            self.counters.failures_deferred += 1
            self._deferred_failure = reason
            log_event(
                logger, logging.INFO, "mqtt_failure_deferred_to_attempt_end", reason=reason
            )
            return
        if self._reconnect_timer is not None:
            # A retry is already armed, so nothing is lost by coalescing. A
            # burst of failure signals must produce one attempt, not one per
            # signal, and must not push the delay further out.
            self.counters.reconnects_coalesced += 1
            log_event(
                logger, logging.INFO, "mqtt_reconnect_already_pending", reason=reason
            )
            return

        delay = 0.0 if immediate else self.reconnect_delay()
        self.counters.reconnects_scheduled += 1
        log_event(
            logger,
            logging.WARNING if not immediate else logging.INFO,
            "mqtt_reconnect_scheduled",
            reason=reason,
            delay_s=round(delay, 3),
            attempt=self._reconnect_attempt,
        )
        self._pending_reason = reason
        self._armed_generation = self._generation
        self._armed_is_retry = not immediate
        self._reconnect_timer = loop.call_later(delay, self._attempt_reconnect)

    def _cancel_reconnect_timer(self) -> None:
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        self._armed_generation = None
        self._armed_is_retry = False

    def _attempt_reconnect(self) -> None:
        self._reconnect_timer = None
        self._armed_generation = None
        self._armed_is_retry = False
        if self._stopping or self._reconnect_task is not None:
            return
        self._reconnect_task = asyncio.create_task(self._run_reconnect())

    async def _run_reconnect(self) -> None:
        """One connection attempt, with the blocking part off the loop.

        ``Client.reconnect()`` does DNS, a TCP connect and a CONNECT write, all
        synchronously. Running it on the event loop would stall ingestion, the
        HTTP API and the WebSocket hub for as long as the broker takes to answer
        — or to time out.
        """
        self.counters.reconnect_attempts += 1
        log_event(
            logger,
            logging.INFO,
            "mqtt_connect_attempt",
            reason=self._pending_reason,
            attempt=self._reconnect_attempt,
        )
        failed = False
        try:
            await asyncio.to_thread(self._connect_blocking)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            self.counters.reconnect_attempt_failures += 1
            log_event(
                logger,
                logging.WARNING,
                "mqtt_reconnect_attempt_failed",
                error=type(exc).__name__,
            )
        finally:
            self._reconnect_task = None

        deferred = self._deferred_failure
        self._deferred_failure = None

        if self._stopping:
            # Shutdown began while this attempt was in flight. Whatever it
            # achieved is undone: a completed reconnect must not revive a
            # session the process has already finished with, and a deferred
            # failure must not turn into a retry either.
            self.counters.reconnects_discarded_after_shutdown += 1
            log_event(logger, logging.INFO, "mqtt_reconnect_discarded_after_shutdown")
            self._teardown_transport()
            return
        if failed:
            # The next attempt is scheduled, never retried inline: the window
            # grows and is jittered again, so this cannot become a tight loop.
            self._schedule_reconnect("reconnect attempt failed")
            return
        if deferred is not None and not self.subscriptions_confirmed:
            # The attempt returned without error, but a definitive failure
            # arrived while it ran and the session never became usable. Without
            # this the adapter would sit with nothing pending and never retry.
            log_event(
                logger, logging.WARNING, "mqtt_deferred_failure_resumed", reason=deferred
            )
            self._schedule_reconnect(deferred)

    def _connect_blocking(self) -> None:
        """Runs in a worker thread. Never touches loop-owned state."""
        self._client.reconnect()
        if self._stopping:
            # Checked again here because shutdown may have started while the
            # socket was being established; starting the network loop now would
            # leave a live thread behind.
            return
        # Paho's loop is started only after a connection exists, so its
        # retry-the-first-connection path is never entered either.
        self._client.loop_start()

    def _teardown_transport(self) -> None:
        with contextlib.suppress(Exception):
            self._client.disconnect()
        with contextlib.suppress(Exception):
            self._client.loop_stop()

    def request_reconnect(self, reason: str) -> None:
        if self._stopping:
            return
        self.counters.reconnects_requested += 1
        self._report_connection(False)
        with contextlib.suppress(Exception):
            self._client.disconnect()
        self._schedule_reconnect(reason)
