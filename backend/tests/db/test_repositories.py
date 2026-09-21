"""Repository and unit-of-work behaviour."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.entities import DeviceStatus, Duplicate, Telemetry
from powerguard.domain.errors import TransientStorageError
from powerguard.domain.services import FixedClock
from tests import builders
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE


def _seed_device(uow_factory: SqlUnitOfWorkFactory, seen_at: dt.datetime) -> None:
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, seen_at)
        uow.commit()


def _insert(uow_factory: SqlUnitOfWorkFactory, telemetry: Telemetry) -> Telemetry | Duplicate:
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(
            telemetry.device_id, FIRMWARE, telemetry.boot_id, telemetry.received_at
        )
        result = uow.telemetry.insert_or_resolve_duplicate(telemetry)
        uow.commit()
        return result


def test_device_upsert_creates_then_updates(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    first = clock.now()
    _seed_device(uow_factory, first)
    with uow_factory() as uow:
        device = uow.devices.get(DEVICE_ID)
    assert device is not None
    assert device.status is DeviceStatus.ONLINE
    assert device.first_seen_at == first
    assert device.last_boot_id == BOOT_ID

    later = first + dt.timedelta(seconds=30)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, "0.2.0", "aaaaaaaa", later)
        uow.commit()
    with uow_factory() as uow:
        device = uow.devices.get(DEVICE_ID)
    assert device is not None
    assert device.first_seen_at == first  # unchanged
    assert device.last_seen_at == later
    assert device.firmware_version == "0.2.0"


def test_last_seen_never_moves_backwards(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    now = clock.now()
    _seed_device(uow_factory, now)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(
            DEVICE_ID, FIRMWARE, BOOT_ID, now - dt.timedelta(seconds=60)
        )
        uow.commit()
    with uow_factory() as uow:
        device = uow.devices.get(DEVICE_ID)
    assert device is not None
    assert device.last_seen_at == now


def test_duplicate_unique_key_resolves_to_existing_row(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    sample = builders.telemetry(seq=7, received_at=clock.now())
    inserted = _insert(uow_factory, sample)
    assert isinstance(inserted, Telemetry)
    assert inserted.id is not None

    # A QoS 1 redelivery: same (device_id, boot_id, seq), later arrival time.
    redelivered = builders.telemetry(seq=7, received_at=clock.now() + dt.timedelta(seconds=2))
    again = _insert(uow_factory, redelivered)
    assert isinstance(again, Duplicate)
    assert again.telemetry_id == inserted.id

    with uow_factory() as uow:
        page = uow.telemetry.history(DEVICE_ID, limit=10)
    assert len(page.items) == 1


def test_rollback_leaves_no_row(uow_factory: SqlUnitOfWorkFactory, clock: FixedClock) -> None:
    _seed_device(uow_factory, clock.now())
    with uow_factory() as uow:
        uow.telemetry.insert_or_resolve_duplicate(builders.telemetry(received_at=clock.now()))
        # Leaving the block without commit must roll back.
    with uow_factory() as uow:
        assert uow.telemetry.latest_for_device(DEVICE_ID) is None


def test_latest_and_history_ordering(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    base = clock.now()
    for n in range(5):
        _insert(uow_factory, builders.telemetry(seq=n, received_at=base + dt.timedelta(seconds=n)))

    with uow_factory() as uow:
        latest = uow.telemetry.latest_for_device(DEVICE_ID)
        page = uow.telemetry.history(DEVICE_ID, limit=10)
    assert latest is not None
    assert latest.seq == 4
    assert [item.seq for item in page.items] == [4, 3, 2, 1, 0]
    assert page.next_before_id is None


def test_history_pagination_is_stable_and_bounded(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    base = clock.now()
    # Same received_at for every row: ordering must fall back to id DESC.
    for n in range(6):
        _insert(uow_factory, builders.telemetry(seq=n, received_at=base))

    with uow_factory() as uow:
        first = uow.telemetry.history(DEVICE_ID, limit=2)
        assert [item.seq for item in first.items] == [5, 4]
        assert first.next_before_id is not None

        second = uow.telemetry.history(DEVICE_ID, limit=2, before_id=first.next_before_id)
        assert [item.seq for item in second.items] == [3, 2]

        third = uow.telemetry.history(DEVICE_ID, limit=2, before_id=second.next_before_id)
        assert [item.seq for item in third.items] == [1, 0]
        # The last page must not advertise another one.
        assert third.next_before_id is None


def test_history_time_window_is_inclusive_then_exclusive(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    base = clock.now()
    for n in range(5):
        _insert(uow_factory, builders.telemetry(seq=n, received_at=base + dt.timedelta(seconds=n)))

    with uow_factory() as uow:
        page = uow.telemetry.history(
            DEVICE_ID,
            limit=10,
            since=base + dt.timedelta(seconds=1),
            until=base + dt.timedelta(seconds=3),
        )
    assert [item.seq for item in page.items] == [2, 1]


def test_history_limit_must_be_positive(uow_factory: SqlUnitOfWorkFactory) -> None:
    with uow_factory() as uow, pytest.raises(ValueError, match="limit"):
        uow.telemetry.history(DEVICE_ID, limit=0)


def test_stale_scan_only_transitions_online_devices(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    now = clock.now()
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry("dev-online", FIRMWARE, BOOT_ID, now)
        uow.devices.upsert_from_telemetry(
            "dev-old", FIRMWARE, BOOT_ID, now - dt.timedelta(minutes=5)
        )
        uow.devices.upsert_from_status(
            "dev-offline", FIRMWARE, BOOT_ID, DeviceStatus.OFFLINE, now - dt.timedelta(minutes=5)
        )
        uow.commit()

    with uow_factory() as uow:
        transitioned = uow.devices.mark_stale(now - dt.timedelta(minutes=1))
        uow.commit()
    assert list(transitioned) == ["dev-old"]

    with uow_factory() as uow:
        assert uow.devices.get("dev-old").status is DeviceStatus.STALE  # type: ignore[union-attr]
        assert uow.devices.get("dev-online").status is DeviceStatus.ONLINE  # type: ignore[union-attr]
        # An explicit offline stays offline.
        assert uow.devices.get("dev-offline").status is DeviceStatus.OFFLINE  # type: ignore[union-attr]


def test_anomaly_insert_and_history(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    inserted = _insert(uow_factory, builders.telemetry(received_at=clock.now()))
    assert isinstance(inserted, Telemetry)
    assert inserted.id is not None

    with uow_factory() as uow:
        stored = uow.anomalies.insert(
            builders.anomaly(telemetry_id=inserted.id, detected_at=clock.now())
        )
        uow.commit()
    assert stored.id is not None
    assert stored.reasons == ("overcurrent_rule",)

    with uow_factory() as uow:
        page = uow.anomalies.history(DEVICE_ID, limit=10)
    assert len(page.items) == 1
    assert page.items[0].score == pytest.approx(0.9)


def test_missing_device_returns_none(uow_factory: SqlUnitOfWorkFactory) -> None:
    with uow_factory() as uow:
        assert uow.devices.get("nobody") is None
        assert uow.telemetry.latest_for_device("nobody") is None
        assert uow.telemetry.history("nobody", limit=5).items == ()


def test_operational_failures_surface_as_transient_storage_errors(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    """A locked or broken database must land on the "do not acknowledge" branch.

    Only ``TransientStorageError`` makes ingestion withhold the acknowledgement,
    so a raw SQLAlchemy error escaping a repository would silently acknowledge
    a message that was never stored.
    """
    with uow_factory() as uow:
        # The table is real; the column is not, which is exactly the shape of an
        # operational failure at flush time.
        uow._session.execute(text("DROP TABLE telemetry"))
        with pytest.raises(TransientStorageError):
            uow.telemetry.insert_or_resolve_duplicate(
                builders.telemetry(received_at=clock.now())
            )


def test_a_failed_read_is_also_transient(uow_factory: SqlUnitOfWorkFactory) -> None:
    with uow_factory() as uow:
        uow._session.execute(text("DROP TABLE devices"))
        with pytest.raises(TransientStorageError):
            uow.devices.list_all()


def test_a_failed_commit_is_transient(uow_factory: SqlUnitOfWorkFactory) -> None:
    with uow_factory() as uow:
        uow._session.execute(text("DROP TABLE anomalies"))
        with pytest.raises(TransientStorageError):
            uow.anomalies.insert(
                builders.anomaly(telemetry_id=1, detected_at=dt.datetime.now(dt.UTC))
            )


def test_anomalies_resolve_for_a_whole_page_of_telemetry(
    uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> None:
    """The REST joins read one query per page, not one per row."""
    ids: list[int] = []
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, clock.now())
        for seq in range(3):
            stored = uow.telemetry.insert_or_resolve_duplicate(
                builders.telemetry(seq=seq, received_at=clock.now() + dt.timedelta(seconds=seq))
            )
            assert isinstance(stored, Telemetry)
            assert stored.id is not None
            ids.append(stored.id)
        uow.anomalies.insert(builders.anomaly(telemetry_id=ids[0], detected_at=clock.now()))
        uow.anomalies.insert(builders.anomaly(telemetry_id=ids[2], detected_at=clock.now()))
        uow.commit()

    with uow_factory() as uow:
        found = uow.anomalies.by_telemetry_ids(ids)
    assert set(found) == {ids[0], ids[2]}
    assert found[ids[0]].method.value == "rule"

    with uow_factory() as uow:
        assert uow.anomalies.by_telemetry_ids([]) == {}
        assert uow.anomalies.by_telemetry_ids([999_999]) == {}
