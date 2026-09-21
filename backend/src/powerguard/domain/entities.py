"""Domain entities and use-case results.

Standard-library types only: nothing here knows about SQLAlchemy, Pydantic,
FastAPI or MQTT. Ingestion results are explicit discriminated outcomes rather
than ``None``/exception ambiguity.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class DeviceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    STALE = "stale"


class AnomalyMethod(StrEnum):
    RULE = "rule"
    ISOLATION_FOREST = "isolation_forest"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    firmware_version: str
    status: DeviceStatus
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    last_boot_id: str | None = None


@dataclass(frozen=True, slots=True)
class Telemetry:
    device_id: str
    boot_id: str
    seq: int
    received_at: dt.datetime
    voltage_v: float
    current_a: float
    power_w: float
    energy_wh: float
    sensor_status: str = "ok"
    sampled_at: dt.datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Anomaly:
    telemetry_id: int
    device_id: str
    detected_at: dt.datetime
    method: AnomalyMethod
    reasons: tuple[str, ...]
    score: float | None = None
    model_version: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class AnomalyVerdict:
    """A positive verdict from an inference adapter. Negatives are not stored."""

    method: AnomalyMethod
    reasons: tuple[str, ...]
    score: float | None = None
    model_version: str | None = None


# --- ingestion outcomes ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ingested:
    telemetry: Telemetry
    anomaly: Anomaly | None = None


@dataclass(frozen=True, slots=True)
class Duplicate:
    telemetry_id: int


@dataclass(frozen=True, slots=True)
class Rejected:
    reason_code: str
    detail: str = ""


IngestOutcome = Ingested | Duplicate | Rejected


# --- pagination -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One bounded page of newest-first rows.

    ``next_before_id`` is set only when a further page may exist, which the
    repository proves by fetching ``limit + 1`` rows.
    """

    items: tuple[T, ...] = field(default_factory=tuple)
    next_before_id: int | None = None
