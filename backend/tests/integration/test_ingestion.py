"""Ingestion outcome matrix, verified end to end without a broker.

Real validation, real repositories and a real migrated SQLite database; only
the MQTT client, the event publisher and the inference engine are injected
fakes.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from powerguard.config import Settings
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.entities import AnomalyMethod, AnomalyVerdict, DeviceStatus
from powerguard.domain.errors import TransientStorageError
from powerguard.domain.services import FixedClock
from powerguard.mqtt.ingestion import IngestionService
from powerguard.mqtt.ports import AckDecision
from powerguard.mqtt.validation import RejectionReason
from tests.fakes.fake_mqtt import FakeInference, FakeMqttClient, RecordingPublisher

DEVICE = "powerguard-01"
BOOT = "7fa31c09"


def telemetry_bytes(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "boot_id": BOOT,
        "seq": 1,
        "sampled_at": None,
        "voltage_v": 7.84,
        "current_a": 0.417,
        "power_w": 3.269,
        "energy_wh": 0.284,
        "sensor_status": "ok",
        "firmware_version": "0.1.0",
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def status_bytes(status: str = "online") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "boot_id": BOOT,
            "firmware_version": "0.1.0",
        }
    ).encode("utf-8")


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def client() -> FakeMqttClient:
    return FakeMqttClient()


def build_service(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    inference: FakeInference | None = None,
) -> IngestionService:
    return IngestionService(
        settings=settings,
        uow_factory=uow_factory,
        clock=clock,
        inference=inference or FakeInference(),
        publisher=publisher,
    )


async def test_accepted_telemetry_persists_broadcasts_and_acks(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    message = client.telemetry_message(DEVICE, telemetry_bytes(), clock.now())

    result = await service.handle(message)
    if result.ack is AckDecision.ACK:
        client.acknowledge(message)

    assert result.outcome == "accepted"
    assert result.telemetry_id is not None
    assert client.acknowledged == [message.mid]
    assert publisher.kinds == ["telemetry"]

    with uow_factory() as uow:
        stored = uow.telemetry.latest_for_device(DEVICE)
        device = uow.devices.get(DEVICE)
    assert stored is not None
    assert stored.seq == 1
    # received_at is server time captured at arrival, not sampled_at.
    assert stored.received_at == clock.now()
    assert stored.sampled_at is None
    assert device is not None
    assert device.status is DeviceStatus.ONLINE
    assert device.last_boot_id == BOOT


async def test_duplicate_delivery_is_acked_without_a_second_row_or_event(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    first = client.telemetry_message(DEVICE, telemetry_bytes(seq=5), clock.now())
    original = await service.handle(first)

    # QoS 1 redelivery: identical key, later arrival.
    clock.advance(2)
    again = client.telemetry_message(DEVICE, telemetry_bytes(seq=5), clock.now())
    result = await service.handle(again)
    if result.ack is AckDecision.ACK:
        client.acknowledge(again)

    assert result.outcome == "duplicate"
    assert result.telemetry_id == original.telemetry_id
    assert again.mid in client.acknowledged
    # Exactly one broadcast, from the first delivery only.
    assert publisher.kinds == ["telemetry"]

    with uow_factory() as uow:
        page = uow.telemetry.history(DEVICE, limit=10)
    assert len(page.items) == 1


async def test_invalid_message_is_acked_to_avoid_a_poison_loop(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    message = client.telemetry_message(DEVICE, b"{not json}", clock.now())

    result = await service.handle(message)
    if result.ack is AckDecision.ACK:
        client.acknowledge(message)

    assert result.outcome == "rejected"
    assert result.reason is RejectionReason.JSON_INVALID
    assert client.acknowledged == [message.mid]
    assert publisher.events == []
    assert service.counters.rejections_by_reason == {"json_invalid": 1}

    with uow_factory() as uow:
        assert uow.devices.get(DEVICE) is None


async def test_transient_storage_failure_is_not_acknowledged(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise TransientStorageError("database is locked")

    monkeypatch.setattr(service, "_persist_telemetry", explode)
    message = client.telemetry_message(DEVICE, telemetry_bytes(), clock.now())

    result = await service.handle(message)
    if result.ack is AckDecision.ACK:
        client.acknowledge(message)

    assert result.ack is AckDecision.WITHHOLD
    assert result.outcome == "transient_failure"
    # Never acknowledged: the broker must redeliver.
    assert client.acknowledged == []
    assert publisher.events == []
    assert service.counters.transient_failures == 1


async def test_anomaly_is_committed_and_broadcast_after_telemetry(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    verdict = AnomalyVerdict(
        method=AnomalyMethod.RULE, reasons=("voltage_drop",), score=0.8
    )
    service = build_service(settings, uow_factory, clock, publisher, FakeInference(verdict))
    message = client.telemetry_message(DEVICE, telemetry_bytes(), clock.now())

    result = await service.handle(message)

    assert result.anomaly_id is not None
    # Telemetry first, then the anomaly.
    assert publisher.kinds == ["telemetry", "anomaly"]

    with uow_factory() as uow:
        page = uow.anomalies.history(DEVICE, limit=10)
    assert len(page.items) == 1
    assert page.items[0].reasons == ("voltage_drop",)
    assert page.items[0].detected_at == clock.now()


async def test_inference_failure_does_not_stop_telemetry(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    broken = FakeInference(failure=RuntimeError("model exploded"))
    service = build_service(settings, uow_factory, clock, publisher, broken)
    message = client.telemetry_message(DEVICE, telemetry_bytes(), clock.now())

    result = await service.handle(message)

    assert result.outcome == "accepted"
    assert result.anomaly_id is None
    assert publisher.kinds == ["telemetry"]
    assert service.counters.inference_failures == 1


async def test_duplicate_never_runs_inference(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    verdict = AnomalyVerdict(method=AnomalyMethod.RULE, reasons=("x",), score=0.5)
    inference = FakeInference(verdict)
    service = build_service(settings, uow_factory, clock, publisher, inference)

    await service.handle(client.telemetry_message(DEVICE, telemetry_bytes(seq=3), clock.now()))
    calls_after_first = inference.calls
    await service.handle(client.telemetry_message(DEVICE, telemetry_bytes(seq=3), clock.now()))

    assert inference.calls == calls_after_first


async def test_status_message_updates_the_device_and_broadcasts(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    message = client.status_message(DEVICE, status_bytes("offline"), clock.now(), retained=True)

    result = await service.handle(message)

    assert result.outcome == "status"
    assert result.ack is AckDecision.ACK
    assert publisher.kinds == ["status"]
    with uow_factory() as uow:
        device = uow.devices.get(DEVICE)
    assert device is not None
    assert device.status is DeviceStatus.OFFLINE


async def test_sequence_gaps_are_counted_but_never_backfilled(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    for seq in (1, 2, 7):
        clock.advance(1)
        await service.handle(
            client.telemetry_message(DEVICE, telemetry_bytes(seq=seq), clock.now())
        )

    assert service.counters.sequence_gaps == 1
    with uow_factory() as uow:
        page = uow.telemetry.history(DEVICE, limit=10)
    # Only the three delivered rows exist; nothing was invented for 3..6.
    assert sorted(item.seq for item in page.items) == [1, 2, 7]


async def test_out_of_order_sequence_is_accepted(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    clock.advance(1)
    await service.handle(client.telemetry_message(DEVICE, telemetry_bytes(seq=9), clock.now()))
    clock.advance(1)
    result = await service.handle(
        client.telemetry_message(DEVICE, telemetry_bytes(seq=4), clock.now())
    )
    assert result.outcome == "accepted"


async def test_received_at_is_server_time_not_payload_time(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    arrival = clock.now()
    payload = telemetry_bytes(sampled_at="2020-01-01T00:00:00.000Z")

    await service.handle(client.telemetry_message(DEVICE, payload, arrival))

    with uow_factory() as uow:
        stored = uow.telemetry.latest_for_device(DEVICE)
    assert stored is not None
    assert stored.received_at == arrival
    assert stored.sampled_at == dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    # Ordering uses the server clock; the stale device timestamp is diagnostic.
    assert stored.received_at > stored.sampled_at


async def test_counters_track_every_outcome(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    publisher: RecordingPublisher,
    client: FakeMqttClient,
) -> None:
    service = build_service(settings, uow_factory, clock, publisher)
    await service.handle(client.telemetry_message(DEVICE, telemetry_bytes(seq=1), clock.now()))
    await service.handle(client.telemetry_message(DEVICE, telemetry_bytes(seq=1), clock.now()))
    await service.handle(client.status_message(DEVICE, status_bytes(), clock.now()))
    await service.handle(client.raw_message("bad/topic", b"{}", clock.now()))

    counters = service.counters
    assert counters.accepted_telemetry == 1
    assert counters.duplicate_telemetry == 1
    assert counters.accepted_status == 1
    assert counters.rejected == 1
    assert counters.rejections_by_reason == {"topic_invalid": 1}
