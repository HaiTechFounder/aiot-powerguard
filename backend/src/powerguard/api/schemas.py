"""REST response DTOs.

Adapter models, shaped exactly as API_CONTRACT.md specifies. Timestamps render
as UTC RFC 3339 with milliseconds and a literal ``Z``.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict

from powerguard.domain.entities import Anomaly, Device, Telemetry

DEFAULT_LIMIT = 500
MIN_LIMIT = 1
MAX_LIMIT = 5000


def rfc3339(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _persisted_id(value: int | None, what: str) -> int:
    """Every row the API serves came from the database, so it has an id.

    Making the DTO field nullable would publish an OpenAPI contract that
    promises clients a null they can never legitimately receive.
    """
    if value is None:
        raise ValueError(f"{what} has no database id; only persisted rows are serialised")
    return value


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnomalySummary(_Dto):
    id: int
    method: str
    score: float | None
    model_version: str | None
    reasons: list[str]

    @classmethod
    def from_domain(cls, anomaly: Anomaly) -> AnomalySummary:
        return cls(
            id=_persisted_id(anomaly.id, "anomaly"),
            method=anomaly.method.value,
            score=anomaly.score,
            model_version=anomaly.model_version,
            reasons=list(anomaly.reasons),
        )


class TelemetryDto(_Dto):
    id: int
    device_id: str
    boot_id: str
    seq: int
    sampled_at: str | None
    received_at: str
    voltage_v: float
    current_a: float
    power_w: float
    energy_wh: float
    sensor_status: str
    anomaly: AnomalySummary | None = None

    @classmethod
    def from_domain(
        cls, telemetry: Telemetry, anomaly: Anomaly | None = None
    ) -> TelemetryDto:
        return cls(
            id=_persisted_id(telemetry.id, "telemetry"),
            device_id=telemetry.device_id,
            boot_id=telemetry.boot_id,
            seq=telemetry.seq,
            sampled_at=rfc3339(telemetry.sampled_at) if telemetry.sampled_at else None,
            received_at=rfc3339(telemetry.received_at),
            voltage_v=telemetry.voltage_v,
            current_a=telemetry.current_a,
            power_w=telemetry.power_w,
            energy_wh=telemetry.energy_wh,
            sensor_status=telemetry.sensor_status,
            anomaly=AnomalySummary.from_domain(anomaly) if anomaly else None,
        )


class DeviceDto(_Dto):
    id: str
    firmware_version: str
    status: str
    first_seen_at: str
    last_seen_at: str
    latest: TelemetryDto | None = None

    @classmethod
    def from_domain(
        cls,
        device: Device,
        latest: Telemetry | None = None,
        anomaly: Anomaly | None = None,
    ) -> DeviceDto:
        return cls(
            id=device.id,
            firmware_version=device.firmware_version,
            status=device.status.value,
            first_seen_at=rfc3339(device.first_seen_at),
            last_seen_at=rfc3339(device.last_seen_at),
            latest=TelemetryDto.from_domain(latest, anomaly) if latest else None,
        )


class DeviceListDto(_Dto):
    items: list[DeviceDto]


class TelemetryPageDto(_Dto):
    items: list[TelemetryDto]
    next_before_id: int | None = None


class AnomalyDto(_Dto):
    """An anomaly plus the measurement it was based on.

    The measurements are required: an anomaly row cannot outlive its telemetry
    row (the foreign key cascades), and both are read in one transaction.
    """

    id: int
    telemetry_id: int
    device_id: str
    detected_at: str
    method: str
    score: float | None
    model_version: str | None
    reasons: list[str]
    voltage_v: float
    current_a: float
    power_w: float
    energy_wh: float

    @classmethod
    def from_domain(cls, anomaly: Anomaly, telemetry: Telemetry) -> AnomalyDto:
        return cls(
            id=_persisted_id(anomaly.id, "anomaly"),
            telemetry_id=anomaly.telemetry_id,
            device_id=anomaly.device_id,
            detected_at=rfc3339(anomaly.detected_at),
            method=anomaly.method.value,
            score=anomaly.score,
            model_version=anomaly.model_version,
            reasons=list(anomaly.reasons),
            voltage_v=telemetry.voltage_v,
            current_a=telemetry.current_a,
            power_w=telemetry.power_w,
            energy_wh=telemetry.energy_wh,
        )


class AnomalyPageDto(_Dto):
    items: list[AnomalyDto]
    next_before_id: int | None = None


class HealthDto(_Dto):
    status: str
    database: str
    mqtt: str
    model: str
    version: str
