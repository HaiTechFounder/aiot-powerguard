"""WebSocket v1 event envelopes.

Every application frame is a v1 envelope carrying an immutable snapshot of a
committed record. Nothing re-queries mutable state at broadcast time, so a
frame always describes the record as it was persisted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from powerguard.domain.entities import Anomaly, Device, Telemetry

SCHEMA_VERSION = 1


class EventType(StrEnum):
    TELEMETRY = "telemetry"
    ANOMALY = "anomaly"
    STATUS = "status"


def _rfc3339(value: dt.datetime) -> str:
    """UTC RFC 3339 with milliseconds, the format the frontend consumes."""
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True, slots=True)
class Event:
    type: EventType
    device_id: str
    emitted_at: dt.datetime
    data: dict[str, Any]

    def envelope(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "type": self.type.value,
            "emitted_at": _rfc3339(self.emitted_at),
            "data": self.data,
        }


def telemetry_event(telemetry: Telemetry, emitted_at: dt.datetime) -> Event:
    return Event(
        type=EventType.TELEMETRY,
        device_id=telemetry.device_id,
        emitted_at=emitted_at,
        data={
            "id": telemetry.id,
            "device_id": telemetry.device_id,
            "boot_id": telemetry.boot_id,
            "seq": telemetry.seq,
            "received_at": _rfc3339(telemetry.received_at),
            "sampled_at": _rfc3339(telemetry.sampled_at) if telemetry.sampled_at else None,
            "voltage_v": telemetry.voltage_v,
            "current_a": telemetry.current_a,
            "power_w": telemetry.power_w,
            "energy_wh": telemetry.energy_wh,
            "sensor_status": telemetry.sensor_status,
        },
    )


def anomaly_event(anomaly: Anomaly, emitted_at: dt.datetime) -> Event:
    return Event(
        type=EventType.ANOMALY,
        device_id=anomaly.device_id,
        emitted_at=emitted_at,
        data={
            "id": anomaly.id,
            "telemetry_id": anomaly.telemetry_id,
            "device_id": anomaly.device_id,
            "detected_at": _rfc3339(anomaly.detected_at),
            "method": anomaly.method.value,
            "score": anomaly.score,
            "model_version": anomaly.model_version,
            "reasons": list(anomaly.reasons),
        },
    )


def status_event(device: Device, emitted_at: dt.datetime) -> Event:
    return Event(
        type=EventType.STATUS,
        device_id=device.id,
        emitted_at=emitted_at,
        data={
            "device_id": device.id,
            "status": device.status.value,
            "last_seen_at": _rfc3339(device.last_seen_at),
        },
    )
