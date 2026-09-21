"""Every device status transition, whatever caused it, on one path.

BACKEND_SPEC section 13 requires a status transition event and counter. The
regression these tests guard is that telemetry-driven recovery —
``stale -> online``, the transition an operator most wants to see — used to
change the database row without reporting anything, because only status
messages went through the reporting path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
from typing import Any

import pytest
from alembic import command

from powerguard.bootstrap import build_container, shutdown_container
from powerguard.config import Settings
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.device_status import DeviceStatusTracker
from powerguard.domain.entities import DeviceStatus
from powerguard.domain.services import FixedClock
from powerguard.mqtt.ingestion import IngestionService
from powerguard.observability import configure_logging
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.conftest import alembic_config, make_settings
from tests.fakes.fake_mqtt import FakeInference, FakeMqttClient, RecordingPublisher
from tests.fakes.fake_transport import FakeMqttTransport

T0 = dt.datetime(2026, 9, 21, 3, 0, 0, tzinfo=dt.UTC)


def telemetry_bytes(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
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
    root = logging.getLogger()
    before = list(root.handlers), root.level
    yield
    root.handlers = before[0]
    root.setLevel(before[1])


@pytest.fixture
def captured() -> io.StringIO:
    return io.StringIO()


def start_capture(captured: io.StringIO) -> None:
    """Installed inside the test: pytest restores handlers after setup."""
    configure_logging("DEBUG", "json", secrets=())
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_powerguard_handler", False):
            handler.setStream(captured)  # type: ignore[attr-defined]


def transitions(captured: io.StringIO) -> list[dict[str, object]]:
    found = []
    for line in captured.getvalue().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:  # pragma: no cover - non-event output
            continue
        if record.get("event") == "device_status_transition":
            found.append(record["fields"])
    return found


def build_service(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    tracker: DeviceStatusTracker | None = None,
) -> IngestionService:
    return IngestionService(
        settings=settings,
        uow_factory=uow_factory,
        clock=clock,
        inference=FakeInference(),
        publisher=RecordingPublisher(),
        status=tracker,
    )


# -- the tracker itself ----------------------------------------------------


def test_only_a_real_change_is_a_transition(captured: io.StringIO) -> None:
    start_capture(captured)
    tracker = DeviceStatusTracker()

    assert tracker.record(DEVICE_ID, DeviceStatus.ONLINE, source="telemetry") is True
    assert tracker.record(DEVICE_ID, DeviceStatus.ONLINE, source="telemetry") is False
    assert tracker.record(DEVICE_ID, DeviceStatus.STALE, source="stale_scan") is True
    assert tracker.record(DEVICE_ID, DeviceStatus.ONLINE, source="telemetry") is True

    assert tracker.counters.status_transitions == 3
    assert tracker.counters.transitions_by_status == {"online": 2, "stale": 1}

    seen = transitions(captured)
    assert [(t["previous"], t["status"]) for t in seen] == [
        ("unknown", "online"),
        ("online", "stale"),
        ("stale", "online"),
    ]
    assert all(t["device_id"] == DEVICE_ID for t in seen)
    assert [t["source"] for t in seen] == ["telemetry", "stale_scan", "telemetry"]


def test_an_unseen_device_transitions_from_unknown(captured: io.StringIO) -> None:
    start_capture(captured)
    tracker = DeviceStatusTracker()

    assert tracker.record(DEVICE_ID, DeviceStatus.ONLINE, source="status_message") is True

    assert transitions(captured)[0]["previous"] == "unknown"
    assert tracker.current(DEVICE_ID) is DeviceStatus.ONLINE


# -- telemetry drives the same path ---------------------------------------


async def test_telemetry_reports_a_stale_device_coming_back(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    """The regression: stale -> online used to be silent."""
    start_capture(captured)
    tracker = DeviceStatusTracker()
    service = build_service(settings, uow_factory, clock, tracker)
    client = FakeMqttClient()

    # The device was online, then the scan marked it stale.
    await service.handle(
        client.telemetry_message(DEVICE_ID, telemetry_bytes(seq=1), T0)
    )
    tracker.record(DEVICE_ID, DeviceStatus.STALE, source="stale_scan")

    await service.handle(
        client.telemetry_message(DEVICE_ID, telemetry_bytes(seq=2), T0)
    )

    seen = transitions(captured)
    assert [(t["previous"], t["status"], t["source"]) for t in seen] == [
        ("unknown", "online", "telemetry"),
        ("online", "stale", "stale_scan"),
        ("stale", "online", "telemetry"),
    ]
    assert tracker.counters.status_transitions == 3

    # The row agrees with the event.
    with uow_factory() as uow:
        device = uow.devices.get(DEVICE_ID)
    assert device is not None
    assert device.status is DeviceStatus.ONLINE


async def test_continuing_telemetry_is_not_a_transition_every_reading(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    tracker = DeviceStatusTracker()
    service = build_service(settings, uow_factory, clock, tracker)
    client = FakeMqttClient()

    for seq in range(5):
        await service.handle(
            client.telemetry_message(DEVICE_ID, telemetry_bytes(seq=seq), T0)
        )

    assert tracker.counters.status_transitions == 1, "online -> online is not a change"
    assert len(transitions(captured)) == 1
    assert service.counters.accepted_telemetry == 5


async def test_a_new_device_announces_itself_through_telemetry(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    tracker = DeviceStatusTracker()
    service = build_service(settings, uow_factory, clock, tracker)
    client = FakeMqttClient()

    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0))

    assert transitions(captured) == [
        {
            "device_id": DEVICE_ID,
            "previous": "unknown",
            "status": "online",
            "source": "telemetry",
            "boot_id": BOOT_ID,
            "retained": False,
        }
    ]


async def test_an_offline_device_recovering_by_telemetry_is_reported(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    tracker = DeviceStatusTracker()
    service = build_service(settings, uow_factory, clock, tracker)
    client = FakeMqttClient()

    # A retained offline LWT, then the device starts publishing again.
    await service.handle(
        client.status_message(DEVICE_ID, status_bytes("offline"), T0, retained=True)
    )
    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0))

    seen = transitions(captured)
    assert [(t["previous"], t["status"], t["source"]) for t in seen] == [
        ("unknown", "offline", "status_message"),
        ("offline", "online", "telemetry"),
    ]
    assert seen[0]["retained"] is True


async def test_status_messages_and_telemetry_share_one_counter(
    settings: Settings,
    uow_factory: SqlUnitOfWorkFactory,
    clock: FixedClock,
    captured: io.StringIO,
) -> None:
    start_capture(captured)
    tracker = DeviceStatusTracker()
    service = build_service(settings, uow_factory, clock, tracker)
    client = FakeMqttClient()

    await service.handle(client.status_message(DEVICE_ID, status_bytes("online"), T0))
    await service.handle(client.telemetry_message(DEVICE_ID, telemetry_bytes(), T0))
    await service.handle(client.status_message(DEVICE_ID, status_bytes("offline"), T0))

    # online (status), then telemetry which changes nothing, then offline.
    assert tracker.counters.status_transitions == 2
    assert len(transitions(captured)) == 2


# -- the stale scan uses it too -------------------------------------------


async def test_the_stale_scan_reports_through_the_same_path(
    database_url: str, uow_factory: SqlUnitOfWorkFactory, captured: io.StringIO
) -> None:
    now = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, now)
        uow.commit()

    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="secret",
        stale_after_s=0.01,
        stale_scan_interval_s=0.01,
    )
    command.upgrade(alembic_config(database_url), "head")
    start_capture(captured)
    transport = FakeMqttTransport()
    container = await build_container(settings, transport_factory=lambda _s: transport)
    try:
        for _ in range(200):
            if container.stale.transitions:
                break
            await asyncio.sleep(0.01)

        assert container.stale.transitions == 1
        assert container.status.counters.status_transitions == 1
        seen = transitions(captured)
        assert seen[0]["source"] == "stale_scan"
        assert seen[0]["status"] == "stale"

        # Repeated scans of an already stale device add nothing.
        await asyncio.sleep(0.05)
        assert container.status.counters.status_transitions == 1
        assert len(transitions(captured)) == 1
    finally:
        await shutdown_container(container)


async def test_the_container_shares_one_tracker_with_ingestion(
    database_url: str,
) -> None:
    """One path means one object, not two that happen to agree."""
    settings = make_settings(database_url, mqtt_enabled=False)
    command.upgrade(alembic_config(database_url), "head")
    container = await build_container(settings)
    try:
        assert container.ingestion.status is container.status
    finally:
        await shutdown_container(container)
