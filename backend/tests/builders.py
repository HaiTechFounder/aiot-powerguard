"""Test data builders."""

from __future__ import annotations

import datetime as dt

from powerguard.domain.entities import Anomaly, AnomalyMethod, Telemetry

DEVICE_ID = "powerguard-01"
BOOT_ID = "7fa31c09"
FIRMWARE = "0.1.0"


def telemetry(
    *,
    seq: int = 1,
    received_at: dt.datetime,
    device_id: str = DEVICE_ID,
    boot_id: str = BOOT_ID,
    voltage_v: float = 7.84,
    current_a: float = 0.417,
    energy_wh: float = 0.284,
    sampled_at: dt.datetime | None = None,
) -> Telemetry:
    return Telemetry(
        device_id=device_id,
        boot_id=boot_id,
        seq=seq,
        received_at=received_at,
        sampled_at=sampled_at,
        voltage_v=voltage_v,
        current_a=current_a,
        power_w=voltage_v * current_a,
        energy_wh=energy_wh,
    )


def anomaly(
    *,
    telemetry_id: int,
    detected_at: dt.datetime,
    device_id: str = DEVICE_ID,
    method: AnomalyMethod = AnomalyMethod.RULE,
    reasons: tuple[str, ...] = ("overcurrent_rule",),
    score: float | None = 0.9,
) -> Anomaly:
    return Anomaly(
        telemetry_id=telemetry_id,
        device_id=device_id,
        detected_at=detected_at,
        method=method,
        reasons=reasons,
        score=score,
    )
