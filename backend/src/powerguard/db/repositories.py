"""SQLAlchemy repositories.

ORM rows stay inside this module: every method returns domain entities. Read
queries fetch ``limit + 1`` rows so a next page can be proven rather than
guessed, and order by ``(received_at DESC, id DESC)`` so pagination is stable
when several rows share a timestamp.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from sqlalchemy import Select, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Executable

from powerguard.db import models
from powerguard.domain.entities import (
    Anomaly,
    AnomalyMethod,
    Device,
    DeviceStatus,
    Duplicate,
    Page,
    Telemetry,
)
from powerguard.domain.errors import TransientStorageError

MAX_PAGE_LIMIT = 1000


def _to_device(row: models.Device) -> Device:
    return Device(
        id=row.id,
        firmware_version=row.firmware_version,
        status=DeviceStatus(row.status),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        last_boot_id=row.last_boot_id,
    )


def _to_telemetry(row: models.Telemetry) -> Telemetry:
    return Telemetry(
        id=row.id,
        device_id=row.device_id,
        boot_id=row.boot_id,
        seq=row.seq,
        sampled_at=row.sampled_at,
        received_at=row.received_at,
        voltage_v=row.voltage_v,
        current_a=row.current_a,
        power_w=row.power_w,
        energy_wh=row.energy_wh,
        sensor_status=row.sensor_status,
    )


def _to_anomaly(row: models.Anomaly) -> Anomaly:
    reasons = tuple(json.loads(row.reasons_json))
    return Anomaly(
        id=row.id,
        telemetry_id=row.telemetry_id,
        device_id=row.device_id,
        detected_at=row.detected_at,
        method=AnomalyMethod(row.method),
        reasons=reasons,
        score=row.score,
        model_version=row.model_version,
    )


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_LIMIT)


class _SqlRepository:
    """Shared SQLAlchemy error translation.

    Every statement a repository issues goes through these helpers, so an
    operational failure — a locked database, a disk error, a dropped connection
    — always surfaces as :class:`TransientStorageError` and therefore always
    lands on the "do not acknowledge, let the broker redeliver" branch of the
    outcome matrix. ``IntegrityError`` is deliberately re-raised untouched:
    only the caller knows whether a constraint violation means "duplicate".
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _translate(self, exc: SQLAlchemyError) -> TransientStorageError:
        # A rollback of an already broken session must not mask the original
        # failure, which is the one worth reporting.
        with contextlib.suppress(SQLAlchemyError):
            self._session.rollback()
        return TransientStorageError(str(exc))

    def _flush(self) -> None:
        try:
            self._session.flush()
        except IntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise self._translate(exc) from exc

    def _execute(self, statement: Executable) -> Any:
        try:
            return self._session.execute(statement)
        except IntegrityError:
            raise
        except SQLAlchemyError as exc:
            raise self._translate(exc) from exc

    def _scalars(self, statement: Select[Any]) -> list[Any]:
        return list(self._execute(statement).scalars())

    def _scalar_or_none(self, statement: Select[Any]) -> Any:
        return self._execute(statement).scalar_one_or_none()

    def _get(self, entity: type[Any], primary_key: Any) -> Any:
        try:
            return self._session.get(entity, primary_key)
        except SQLAlchemyError as exc:
            raise self._translate(exc) from exc


class SqlDeviceRepository(_SqlRepository):
    def _upsert(
        self,
        device_id: str,
        firmware_version: str,
        boot_id: str | None,
        seen_at: dt.datetime,
        status: DeviceStatus,
    ) -> Device:
        row = self._get(models.Device, device_id)
        if row is None:
            row = models.Device(
                id=device_id,
                firmware_version=firmware_version,
                status=status.value,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
                last_boot_id=boot_id,
            )
            self._session.add(row)
        else:
            row.firmware_version = firmware_version
            row.status = status.value
            # Never move last_seen_at backwards: out-of-order delivery must not
            # make a live device look older than it is.
            if seen_at > row.last_seen_at:
                row.last_seen_at = seen_at
            if boot_id is not None:
                row.last_boot_id = boot_id
        self._flush()
        return _to_device(row)

    def upsert_from_telemetry(
        self, device_id: str, firmware_version: str, boot_id: str, seen_at: dt.datetime
    ) -> Device:
        return self._upsert(device_id, firmware_version, boot_id, seen_at, DeviceStatus.ONLINE)

    def upsert_from_status(
        self,
        device_id: str,
        firmware_version: str,
        boot_id: str,
        status: DeviceStatus,
        seen_at: dt.datetime,
    ) -> Device:
        return self._upsert(device_id, firmware_version, boot_id, seen_at, status)

    def get(self, device_id: str) -> Device | None:
        row = self._get(models.Device, device_id)
        return _to_device(row) if row is not None else None

    def list_all(self) -> Sequence[Device]:
        rows = self._scalars(select(models.Device).order_by(models.Device.id.asc()))
        return [_to_device(row) for row in rows]

    def mark_stale(self, cutoff: dt.datetime) -> Sequence[str]:
        # Only currently online rows go stale. An explicit offline stays offline.
        ids = self._scalars(
            select(models.Device.id).where(
                models.Device.status == DeviceStatus.ONLINE.value,
                models.Device.last_seen_at < cutoff,
            )
        )
        if not ids:
            return []
        self._execute(
            update(models.Device)
            .where(models.Device.id.in_(ids))
            .values(status=DeviceStatus.STALE.value)
        )
        self._flush()
        return [str(device_id) for device_id in ids]


class SqlTelemetryRepository(_SqlRepository):
    def insert_or_resolve_duplicate(self, telemetry: Telemetry) -> Telemetry | Duplicate:
        row = models.Telemetry(
            device_id=telemetry.device_id,
            boot_id=telemetry.boot_id,
            seq=telemetry.seq,
            sampled_at=telemetry.sampled_at,
            received_at=telemetry.received_at,
            voltage_v=telemetry.voltage_v,
            current_a=telemetry.current_a,
            power_w=telemetry.power_w,
            energy_wh=telemetry.energy_wh,
            sensor_status=telemetry.sensor_status,
        )
        self._session.add(row)
        try:
            # The unique constraint is the authority for idempotency; a
            # pre-insert existence check would not be race-safe. Non-integrity
            # failures are already translated by _flush().
            self._flush()
        except IntegrityError as exc:
            self._session.rollback()
            if not _is_duplicate_key(exc):
                raise TransientStorageError(str(exc.orig)) from exc
            existing = self._scalar_or_none(
                select(models.Telemetry.id).where(
                    models.Telemetry.device_id == telemetry.device_id,
                    models.Telemetry.boot_id == telemetry.boot_id,
                    models.Telemetry.seq == telemetry.seq,
                )
            )
            if existing is None:
                # The conflict was not the idempotency key after all.
                raise TransientStorageError("unique conflict without a matching row") from exc
            return Duplicate(telemetry_id=int(existing))
        return _to_telemetry(row)

    def by_id(self, telemetry_id: int) -> Telemetry | None:
        row = self._get(models.Telemetry, telemetry_id)
        return _to_telemetry(row) if row is not None else None

    def latest_for_device(self, device_id: str) -> Telemetry | None:
        row = self._scalar_or_none(
            select(models.Telemetry)
            .where(models.Telemetry.device_id == device_id)
            .order_by(models.Telemetry.received_at.desc(), models.Telemetry.id.desc())
            .limit(1)
        )
        return _to_telemetry(row) if row is not None else None

    def history(
        self,
        device_id: str,
        *,
        limit: int,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        before_id: int | None = None,
    ) -> Page[Telemetry]:
        bounded = _clamp_limit(limit)
        stmt = select(models.Telemetry).where(models.Telemetry.device_id == device_id)
        if since is not None:
            stmt = stmt.where(models.Telemetry.received_at >= since)
        if until is not None:
            stmt = stmt.where(models.Telemetry.received_at < until)
        if before_id is not None:
            stmt = stmt.where(models.Telemetry.id < before_id)
        stmt = stmt.order_by(
            models.Telemetry.received_at.desc(), models.Telemetry.id.desc()
        ).limit(bounded + 1)

        rows = self._scalars(stmt)
        return _build_page(rows, bounded, _to_telemetry)


class SqlAnomalyRepository(_SqlRepository):
    def insert(self, anomaly: Anomaly) -> Anomaly:
        row = models.Anomaly(
            telemetry_id=anomaly.telemetry_id,
            device_id=anomaly.device_id,
            detected_at=anomaly.detected_at,
            method=anomaly.method.value,
            score=anomaly.score,
            model_version=anomaly.model_version,
            reasons_json=json.dumps(list(anomaly.reasons)),
        )
        self._session.add(row)
        try:
            self._flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise TransientStorageError(str(exc.orig)) from exc
        return _to_anomaly(row)

    def by_telemetry_id(self, telemetry_id: int) -> Anomaly | None:
        row = self._scalar_or_none(
            select(models.Anomaly).where(models.Anomaly.telemetry_id == telemetry_id)
        )
        return _to_anomaly(row) if row is not None else None

    def by_telemetry_ids(self, telemetry_ids: Sequence[int]) -> dict[int, Anomaly]:
        """Anomalies for a page of telemetry rows, keyed by telemetry id.

        API_CONTRACT requires the stored verdict to travel with the reading it
        belongs to, so history and `latest` resolve it in one query instead of
        reporting `null` for rows that do have an anomaly.
        """
        if not telemetry_ids:
            return {}
        rows = self._scalars(
            select(models.Anomaly).where(models.Anomaly.telemetry_id.in_(list(telemetry_ids)))
        )
        return {int(row.telemetry_id): _to_anomaly(row) for row in rows}

    def history(
        self,
        device_id: str,
        *,
        limit: int,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        before_id: int | None = None,
    ) -> Page[Anomaly]:
        bounded = _clamp_limit(limit)
        stmt = select(models.Anomaly).where(models.Anomaly.device_id == device_id)
        if since is not None:
            stmt = stmt.where(models.Anomaly.detected_at >= since)
        if until is not None:
            stmt = stmt.where(models.Anomaly.detected_at < until)
        if before_id is not None:
            stmt = stmt.where(models.Anomaly.id < before_id)
        stmt = stmt.order_by(
            models.Anomaly.detected_at.desc(), models.Anomaly.id.desc()
        ).limit(bounded + 1)

        rows = self._scalars(stmt)
        return _build_page(rows, bounded, _to_anomaly)


Row = TypeVar("Row")
Item = TypeVar("Item", Telemetry, Anomaly)


def _build_page(
    rows: list[Row], limit: int, convert: Callable[[Row], Item]
) -> Page[Item]:
    """Trim the probe row and only advertise a next page when one is proven."""
    has_more = len(rows) > limit
    items = tuple(convert(row) for row in rows[:limit])
    next_before_id = items[-1].id if (has_more and items) else None
    return Page(items=items, next_before_id=next_before_id)


def _is_duplicate_key(exc: IntegrityError) -> bool:
    message = str(exc.orig).lower()
    return "unique" in message
