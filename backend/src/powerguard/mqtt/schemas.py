"""Strict Pydantic models for the MQTT v1 payloads.

Adapter DTOs, not domain entities. Every model forbids unknown fields and uses
strict primitives, so a JSON boolean is never accepted where an integer is
required and a JSON string is never coerced into a number.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

BOOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
SEQ_MAX = 4_294_967_295
FIRMWARE_VERSION_MAX = 32

# RFC 3339 UTC with milliseconds, as the firmware will emit once NTP exists.
SAMPLED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def parse_sampled_at(value: str) -> dt.datetime | None:
    """Parse a validated ``sampled_at`` string, or None if it is not a date.

    A syntactically correct string can still be an impossible calendar value
    (month 13, 31 February, a non-leap 29 February). Those are deterministic
    rejections, never ingestion-time exceptions.
    """
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC)

class _V1Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]


def _reject_bool(value: object) -> None:
    # bool is a subclass of int, so Pydantic would otherwise accept JSON true
    # as the number 1.
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")


def _validate_boot_id(value: str) -> str:
    if not BOOT_ID_PATTERN.match(value):
        raise ValueError("boot_id must match ^[0-9a-f]{8}$")
    return value


def _validate_firmware_version(value: str) -> str:
    if len(value) > FIRMWARE_VERSION_MAX:
        raise ValueError(f"firmware_version longer than {FIRMWARE_VERSION_MAX} characters")
    if not SEMVER_PATTERN.match(value):
        raise ValueError("firmware_version must be SemVer")
    return value


class TelemetryPayloadV1(_V1Base):
    """Exactly the telemetry fields and rules in MQTT_SPEC.md."""

    boot_id: StrictStr
    seq: StrictInt
    sampled_at: StrictStr | None
    voltage_v: float
    current_a: float
    power_w: float
    energy_wh: float
    sensor_status: Literal["ok"]
    firmware_version: StrictStr

    _check_boot_id = field_validator("boot_id")(staticmethod(_validate_boot_id))
    _check_version = field_validator("firmware_version")(staticmethod(_validate_firmware_version))

    @field_validator("seq")
    @classmethod
    def _check_seq(cls, value: int) -> int:
        if not 0 <= value <= SEQ_MAX:
            raise ValueError("seq outside unsigned 32-bit range")
        return value

    @field_validator("voltage_v", "current_a", "power_w", "energy_wh", mode="before")
    @classmethod
    def _reject_boolean_numbers(cls, value: object) -> object:
        _reject_bool(value)
        return value

    @field_validator("voltage_v", "current_a", "power_w", "energy_wh")
    @classmethod
    def _require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("measurement must be finite")
        return value

    @field_validator("energy_wh")
    @classmethod
    def _require_non_negative_energy(cls, value: float) -> float:
        if value < 0:
            raise ValueError("energy_wh must be non-negative")
        return value

    @field_validator("sampled_at")
    @classmethod
    def _check_sampled_at(cls, value: str | None) -> str | None:
        # The current firmware always sends null; a timestamp must still be a
        # UTC RFC 3339 value with milliseconds when it appears. The grammar
        # alone is not enough: "2026-02-31T00:00:00.000Z" matches the pattern
        # but is not a date, so it is parsed here and rejected deterministically
        # instead of raising later inside ingestion.
        if value is None:
            return None
        if not SAMPLED_AT_PATTERN.match(value):
            raise ValueError("sampled_at must be UTC RFC 3339 with milliseconds")
        if parse_sampled_at(value) is None:
            raise ValueError("sampled_at is not a real UTC calendar timestamp")
        return value

    def sampled_at_datetime(self) -> dt.datetime | None:
        """The validated timestamp. Validation already proved it parses."""
        if self.sampled_at is None:
            return None
        parsed = parse_sampled_at(self.sampled_at)
        if parsed is None:  # pragma: no cover - unreachable after validation
            raise ValueError("sampled_at is not a real UTC calendar timestamp")
        return parsed


class StatusPayloadV1(_V1Base):
    """Exactly the four approved status fields."""

    status: Literal["online", "offline"]
    boot_id: StrictStr
    firmware_version: StrictStr

    _check_boot_id = field_validator("boot_id")(staticmethod(_validate_boot_id))
    _check_version = field_validator("firmware_version")(staticmethod(_validate_firmware_version))
