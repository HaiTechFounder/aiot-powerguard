"""Device staleness, and the counters that describe the running process."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from alembic import command

from powerguard.bootstrap import (
    StaleCounters,
    _mark_stale,
    _stale_scan_loop,
    build_container,
    shutdown_container,
)
from powerguard.config import Settings
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.entities import DeviceStatus
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.conftest import alembic_config, make_settings
from tests.fakes.fake_transport import FakeMqttTransport

OTHER = "powerguard-02"


@pytest.fixture
def seeded_devices(uow_factory: SqlUnitOfWorkFactory) -> dt.datetime:
    # Real time, because the scan loop runs on the system clock.
    now = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, now)
        uow.devices.upsert_from_status(
            OTHER, FIRMWARE, BOOT_ID, DeviceStatus.OFFLINE, now
        )
        uow.commit()
    return now


async def test_only_silent_online_devices_go_stale(
    settings: Settings, uow_factory: SqlUnitOfWorkFactory, seeded_devices: dt.datetime
) -> None:
    container = type("C", (), {"uow_factory": uow_factory})()
    cutoff = seeded_devices + dt.timedelta(seconds=30)

    transitioned = _mark_stale(container, cutoff)  # type: ignore[arg-type]

    assert [device.id for device in transitioned] == [DEVICE_ID]
    assert transitioned[0].status is DeviceStatus.STALE
    with uow_factory() as uow:
        # An explicit offline is a fact from the device and stays untouched.
        other = uow.devices.get(OTHER)
        assert other is not None
        assert other.status is DeviceStatus.OFFLINE
    del settings


async def test_a_device_that_is_still_reporting_is_left_alone(
    uow_factory: SqlUnitOfWorkFactory, seeded_devices: dt.datetime
) -> None:
    container = type("C", (), {"uow_factory": uow_factory})()
    cutoff = seeded_devices - dt.timedelta(seconds=30)

    assert _mark_stale(container, cutoff) == []  # type: ignore[arg-type]


async def test_the_scan_loop_broadcasts_only_real_transitions(
    database_url: str, seeded_devices: dt.datetime
) -> None:
    settings = make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="secret",
        stale_after_s=0.01,
        stale_scan_interval_s=0.01,
    )
    command.upgrade(alembic_config(database_url), "head")
    transport = FakeMqttTransport()
    container = await build_container(settings, transport_factory=lambda _s: transport)
    try:
        for _ in range(200):
            if container.hub.counters.events_published:
                break
            await asyncio.sleep(0.01)
        published = container.hub.counters.events_published
        assert published >= 1

        # The transition happened once; the scan does not re-announce it.
        await asyncio.sleep(0.05)
        assert container.hub.counters.events_published == published
    finally:
        await shutdown_container(container)


async def test_a_failing_scan_does_not_kill_the_loop(
    settings: Settings, uow_factory: SqlUnitOfWorkFactory
) -> None:
    """A scan error is counted and retried; HTTP and ingestion keep running."""
    calls = 0

    class ExplodingFactory:
        def __call__(self) -> object:
            nonlocal calls
            calls += 1
            raise RuntimeError("database gone")

    container = type(
        "C",
        (),
        {
            "settings": make_settings(settings.database_url, stale_scan_interval_s=0.01),
            "uow_factory": ExplodingFactory(),
            "clock": type("K", (), {"now": staticmethod(lambda: dt.datetime.now(dt.UTC))})(),
            "hub": None,
            "stale": StaleCounters(),
        },
    )()

    task = asyncio.ensure_future(_stale_scan_loop(container))  # type: ignore[arg-type]
    for _ in range(200):
        if calls >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 2, "the loop retried after the failure instead of dying"
    assert container.stale.failures >= 2
    assert container.stale.transitions == 0
    del uow_factory


async def test_stale_counters_describe_what_the_scan_did(
    database_url: str, seeded_devices: dt.datetime
) -> None:
    """BACKEND_SPEC 13 requires stale transitions to be counted, not just logged."""
    settings = make_settings(
        database_url,
        mqtt_enabled=False,
        stale_after_s=0.01,
        stale_scan_interval_s=0.01,
    )
    command.upgrade(alembic_config(database_url), "head")
    container = await build_container(settings)
    try:
        for _ in range(200):
            if container.stale.transitions:
                break
            await asyncio.sleep(0.01)

        assert container.stale.scans >= 1
        assert container.stale.transitions == 1, "one device went stale, exactly once"
        assert container.stale.failures == 0

        # Repeated scans must not re-count a device that is already stale.
        await asyncio.sleep(0.05)
        assert container.stale.transitions == 1
    finally:
        await shutdown_container(container)
