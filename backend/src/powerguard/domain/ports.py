"""Ports the domain depends on. Adapters implement these protocols."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, runtime_checkable

from powerguard.domain.entities import (
    Anomaly,
    AnomalyVerdict,
    Device,
    DeviceStatus,
    Duplicate,
    Page,
    Telemetry,
)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> dt.datetime:
        """Current time, timezone-aware UTC."""
        ...


class DeviceRepository(Protocol):
    def upsert_from_telemetry(
        self, device_id: str, firmware_version: str, boot_id: str, seen_at: dt.datetime
    ) -> Device: ...

    def upsert_from_status(
        self,
        device_id: str,
        firmware_version: str,
        boot_id: str,
        status: DeviceStatus,
        seen_at: dt.datetime,
    ) -> Device: ...

    def get(self, device_id: str) -> Device | None: ...

    def list_all(self) -> Sequence[Device]: ...

    def mark_stale(self, cutoff: dt.datetime) -> Sequence[str]:
        """Move currently online devices last seen before cutoff to stale.

        Returns the ids actually transitioned, so only real transitions are
        broadcast.
        """
        ...


class TelemetryRepository(Protocol):
    def insert_or_resolve_duplicate(self, telemetry: Telemetry) -> Telemetry | Duplicate:
        """Insert, or resolve the existing row when the unique key conflicts."""
        ...

    def by_id(self, telemetry_id: int) -> Telemetry | None: ...

    def latest_for_device(self, device_id: str) -> Telemetry | None: ...

    def history(
        self,
        device_id: str,
        *,
        limit: int,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        before_id: int | None = None,
    ) -> Page[Telemetry]:
        """Newest-first page. ``since`` inclusive, ``until`` exclusive."""
        ...


class AnomalyRepository(Protocol):
    def insert(self, anomaly: Anomaly) -> Anomaly: ...

    def by_telemetry_id(self, telemetry_id: int) -> Anomaly | None: ...

    def by_telemetry_ids(self, telemetry_ids: Sequence[int]) -> dict[int, Anomaly]:
        """Stored verdicts for a page of telemetry rows, keyed by telemetry id."""
        ...

    def history(
        self,
        device_id: str,
        *,
        limit: int,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        before_id: int | None = None,
    ) -> Page[Anomaly]: ...


class UnitOfWork(Protocol):
    devices: DeviceRepository
    telemetry: TelemetryRepository
    anomalies: AnomalyRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class InferenceEngine(Protocol):
    def readiness(self) -> str:
        """``ready`` or ``unavailable``."""
        ...

    def evaluate(self, telemetry: Telemetry) -> AnomalyVerdict | None:
        """Return a positive verdict, or None when nothing is flagged."""
        ...


class EventPublisher(Protocol):
    async def publish_telemetry(self, telemetry: Telemetry) -> None: ...

    async def publish_anomaly(self, anomaly: Anomaly) -> None: ...

    async def publish_device_status(self, device: Device) -> None: ...
