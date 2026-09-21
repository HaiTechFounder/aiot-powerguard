"""The event and counter categories BACKEND_SPEC section 13 requires.

Asserted on rendered output and on counter values, so "structured logging
exists" is a fact rather than a claim. Redaction is re-checked here because
adding events is exactly when a payload or a credential slips into a log line.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging

import pytest

from powerguard.config import Settings
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.entities import AnomalyMethod, AnomalyVerdict, DeviceStatus
from powerguard.domain.errors import TransientStorageError
from powerguard.domain.services import FixedClock
from powerguard.mqtt.ingestion import IngestionService
from powerguard.observability import REDACTED, configure_logging
from powerguard.realtime.hub import EventHub
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.fakes.fake_mqtt import FakeInference, FakeMqttClient, RecordingPublisher

SECRET = "broker-password-value"
T0 = dt.datetime(2026, 9, 21, 3, 0, 0, tzinfo=dt.UTC)


def telemetry_bytes(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "schema_version": 1,
        "boot_id": BOOT_ID,
        "seq": 1,
        "sampled_at": None,
        "voltage_v": 7.84,
        "current_a": 0.417,
        "power_w": 3.269,
        "energy_wh": 0.284,
        "sensor_status": "ok",
        "firmware_version": FIRMWARE,
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def status_bytes(status: str = "online") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "boot_id": BOOT_ID,
            "firmware_version": FIRMWARE,
        }
    ).encode("utf-8")


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    """Put the process log configuration back after each test."""
    root = logging.getLogger()
    before = list(root.handlers), root.level
    yield
    root.handlers = before[0]
    root.setLevel(before[1])


@pytest.fixture
def captured() -> io.StringIO:
    """A capture buffer. `start_capture` attaches it to the real handler.

    The handler has to be installed inside the test body: pytest's logging
    plugin restores the root handlers at the end of the setup phase, so a
    handler added by a fixture would be gone before the test runs.
    """
    return io.StringIO()


def start_capture(captured: io.StringIO) -> None:
    configure_logging("DEBUG", "json", secrets=(SECRET,))
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_powerguard_handler", False):
            handler.setStream(captured)  # type: ignore[attr-defined]


def events(captured: io.StringIO) -> list[dict[str, object]]:
    records = []
    for line in captured.getvalue().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - non-event output
            continue
    return records


def names(captured: io.StringIO) -> list[str]:
    return [str(record["event"]) for record in events(captured)]


def build_service(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    inference: FakeInference,
    publisher: RecordingPublisher,
) -> IngestionService:
    return IngestionService(
        settings=settings,
        uow_factory=uow_factory,
        clock=clock,
        inference=inference,
        publisher=publisher,
    )


# -- ingest category -------------------------------------------------------


async def test_accepted_duplicate_and_rejected_each_emit_their_event(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )
    payload = telemetry_bytes(seq=4)

    await service.handle(client.telemetry_message(DEVICE_ID, payload, T0))
    await service.handle(client.telemetry_message(DEVICE_ID, payload, T0))
    await service.handle(client.telemetry_message(DEVICE_ID, b"{not json", T0))

    emitted = names(captured)
    assert "ingest_accepted" in emitted
    assert "ingest_duplicate" in emitted
    assert "ingest_rejected" in emitted

    assert service.counters.accepted_telemetry == 1
    assert service.counters.duplicate_telemetry == 1
    assert service.counters.rejections_by_reason == {"json_invalid": 1}

    accepted = next(r for r in events(captured) if r["event"] == "ingest_accepted")
    fields = accepted["fields"]
    assert fields["device_id"] == DEVICE_ID  # type: ignore[index]
    assert fields["seq"] == 4  # type: ignore[index]
    assert isinstance(fields["telemetry_id"], int)  # type: ignore[index]


async def test_a_rejection_event_never_carries_payload_content(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )

    await service.handle(
        client.telemetry_message(DEVICE_ID, telemetry_bytes(voltage_v=99.0), T0)
    )

    output = captured.getvalue()
    assert "ingest_rejected" in output
    assert "99.0" not in output, "a bound is reported, never the offending value"
    assert service.counters.rejections_by_reason == {"measurement_out_of_range": 1}


async def test_a_storage_failure_emits_its_own_event_and_counter(
    settings: Settings, clock: FixedClock, captured: io.StringIO
) -> None:
    start_capture(captured)
    class BrokenFactory:
        def __call__(self) -> object:
            raise TransientStorageError("database is locked")

    client = FakeMqttClient()
    service = build_service(
        settings,
        BrokenFactory(),  # type: ignore[arg-type]
        clock,
        FakeInference(),
        RecordingPublisher(),
    )

    result = await service.handle(
        client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0)
    )

    assert result.ack.value == "withhold"
    assert service.counters.transient_failures == 1
    assert "ingest_storage_failure" in names(captured)


async def test_a_sequence_gap_is_counted_and_named(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )

    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(seq=1), T0))
    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(seq=9), T0))

    assert service.counters.sequence_gaps == 1
    gap = next(r for r in events(captured) if r["event"] == "ingest_sequence_gap")
    assert gap["fields"]["from_seq"] == 1  # type: ignore[index]
    assert gap["fields"]["to_seq"] == 9  # type: ignore[index]


# -- inference category ----------------------------------------------------


async def test_a_positive_verdict_is_counted_and_named(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    verdict = AnomalyVerdict(
        method=AnomalyMethod.RULE, reasons=("overcurrent_rule",), score=0.91
    )
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(verdict), RecordingPublisher()
    )

    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0))

    assert service.counters.anomalies_detected == 1
    detected = next(r for r in events(captured) if r["event"] == "anomaly_detected")
    assert detected["fields"]["method"] == "rule"  # type: ignore[index]
    assert isinstance(detected["fields"]["anomaly_id"], int)  # type: ignore[index]


async def test_an_unavailable_model_is_counted_separately_from_a_failure(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )

    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0))

    assert service.counters.inference_unavailable == 1
    assert service.counters.inference_failures == 0
    assert service.counters.anomalies_detected == 0
    assert "anomaly_detected" not in names(captured)


async def test_an_inference_failure_is_counted_and_named(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings,
        uow_factory,
        clock,
        FakeInference(failure=RuntimeError("model exploded")),
        RecordingPublisher(),
    )

    result = await service.handle(
        client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0)
    )

    assert result.outcome == "accepted", "the reading survives a broken model"
    assert service.counters.inference_failures == 1
    assert "inference_failed" in names(captured)


# -- device status category ------------------------------------------------


async def test_a_status_transition_is_counted_once(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )

    await service.handle(client.status_message(DEVICE_ID, status_bytes("online"), T0))
    await service.handle(client.status_message(DEVICE_ID, status_bytes("online"), T0))
    await service.handle(client.status_message(DEVICE_ID, status_bytes("offline"), T0))

    assert service.counters.accepted_status == 3
    # online -> offline is one transition; the repeat is not.
    assert service.status.counters.status_transitions == 2

    transitions = [
        r for r in events(captured) if r["event"] == "device_status_transition"
    ]
    assert [t["fields"]["status"] for t in transitions] == ["online", "offline"]
    assert transitions[1]["fields"]["previous"] == "online"  # type: ignore[index]


async def test_a_retained_status_is_marked_as_such(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )

    await service.handle(
        client.status_message(DEVICE_ID, status_bytes("offline"), T0, retained=True)
    )

    transition = next(
        r for r in events(captured) if r["event"] == "device_status_transition"
    )
    assert transition["fields"]["retained"] is True  # type: ignore[index]
    assert transition["fields"]["status"] == DeviceStatus.OFFLINE.value  # type: ignore[index]


# -- websocket category ----------------------------------------------------


async def test_websocket_open_close_and_drop_are_named(
    clock: FixedClock, captured: io.StringIO
) -> None:
    start_capture(captured)
    from tests import builders

    hub = EventHub(clock=clock, max_connections=1, queue_size=1)
    subscription = hub.subscribe(DEVICE_ID)

    with pytest.raises(Exception, match="cap"):
        hub.subscribe(DEVICE_ID)

    for seq in range(3):
        await hub.publish_telemetry(builders.telemetry(seq=seq, received_at=clock.now()))

    hub.unsubscribe(subscription)

    emitted = names(captured)
    assert "websocket_opened" in emitted
    assert "websocket_rejected" in emitted
    assert "websocket_slow_client_dropped" in emitted
    assert hub.counters.opened == 1
    assert hub.counters.rejected == 1
    assert hub.counters.slow_clients_dropped == 1
    assert hub.counters.closed == 1


# -- redaction still holds -------------------------------------------------


async def test_no_event_leaks_the_broker_password_or_a_payload(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    client = FakeMqttClient()
    service = build_service(
        settings, uow_factory, clock, FakeInference(), RecordingPublisher()
    )

    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0))
    await service.handle(
        client.telemetry_message(DEVICE_ID, f'{{"p":"{SECRET}"}}'.encode(), T0)
    )
    logging.getLogger("third.party").warning("connecting with %s", SECRET)

    output = captured.getvalue()
    assert SECRET not in output
    assert REDACTED in output
    assert "energy_wh" not in output, "no payload field names reach a log line"
