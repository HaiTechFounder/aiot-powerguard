"""Validation of an inbound MQTT message.

Returns controlled reason codes and never echoes payload content: a rejected
message may be malformed, oversized or hostile, so only its category, size and
topic are ever reported.

Pure functions. Nothing here touches the database, the broker or the clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from powerguard.config import Settings
from powerguard.mqtt.schemas import StatusPayloadV1, TelemetryPayloadV1
from powerguard.mqtt.topics import ParsedTopic, TopicKind, parse_topic


class RejectionReason(StrEnum):
    TOPIC_INVALID = "topic_invalid"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UTF8_INVALID = "utf8_invalid"
    JSON_INVALID = "json_invalid"
    SCHEMA_INVALID = "schema_invalid"
    VERSION_UNSUPPORTED = "version_unsupported"
    MEASUREMENT_OUT_OF_RANGE = "measurement_out_of_range"
    POWER_INCONSISTENT = "power_inconsistent"


@dataclass(frozen=True, slots=True)
class ValidTelemetry:
    topic: ParsedTopic
    payload: TelemetryPayloadV1


@dataclass(frozen=True, slots=True)
class ValidStatus:
    topic: ParsedTopic
    payload: StatusPayloadV1
    retained: bool = False


@dataclass(frozen=True, slots=True)
class Invalid:
    reason: RejectionReason
    # A short, non-echoing hint: a field name or a bound, never a payload value.
    detail: str = ""


ValidationResult = ValidTelemetry | ValidStatus | Invalid


def _schema_reason(error: ValidationError) -> tuple[RejectionReason, str]:
    """Map a Pydantic failure to a controlled category and a field name."""
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        if location == "schema_version":
            return RejectionReason.VERSION_UNSUPPORTED, location
        return RejectionReason.SCHEMA_INVALID, location
    return RejectionReason.SCHEMA_INVALID, ""


def _check_measurement_bounds(
    payload: TelemetryPayloadV1, settings: Settings
) -> Invalid | None:
    """Structural measurement-integrity bounds, not operating thresholds."""
    if not settings.min_voltage_v <= payload.voltage_v <= settings.max_voltage_v:
        return Invalid(RejectionReason.MEASUREMENT_OUT_OF_RANGE, "voltage_v")
    if abs(payload.current_a) > settings.max_abs_current_a:
        return Invalid(RejectionReason.MEASUREMENT_OUT_OF_RANGE, "current_a")
    if abs(payload.power_w) > settings.max_abs_power_w:
        return Invalid(RejectionReason.MEASUREMENT_OUT_OF_RANGE, "power_w")
    return None


def _check_power_consistency(
    payload: TelemetryPayloadV1, settings: Settings
) -> Invalid | None:
    """Compare reported power with V x I. The stored value stays as reported."""
    tolerance = settings.power_rel_tolerance
    if tolerance <= 0:
        return None
    expected = payload.voltage_v * payload.current_a
    allowed = abs(expected) * tolerance
    # An absolute floor keeps near-zero readings from failing on rounding.
    allowed = max(allowed, 0.05)
    if abs(payload.power_w - expected) > allowed:
        return Invalid(RejectionReason.POWER_INCONSISTENT, "power_w")
    return None


def validate_message(
    topic: str, payload: bytes, settings: Settings, *, retained: bool = False
) -> ValidationResult:
    parsed = parse_topic(topic)
    if parsed is None:
        return Invalid(RejectionReason.TOPIC_INVALID)

    # Size is checked before decoding or parsing: an oversized payload is never
    # handed to the JSON parser.
    if parsed.kind is TopicKind.TELEMETRY and len(payload) > settings.max_payload_bytes:
        return Invalid(
            RejectionReason.PAYLOAD_TOO_LARGE, f"limit={settings.max_payload_bytes}"
        )
    if parsed.kind is TopicKind.STATUS and len(payload) > settings.max_payload_bytes:
        return Invalid(
            RejectionReason.PAYLOAD_TOO_LARGE, f"limit={settings.max_payload_bytes}"
        )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return Invalid(RejectionReason.UTF8_INVALID)

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return Invalid(RejectionReason.JSON_INVALID)

    if not isinstance(document, dict):
        return Invalid(RejectionReason.SCHEMA_INVALID, "root")

    if parsed.kind is TopicKind.TELEMETRY:
        try:
            telemetry = TelemetryPayloadV1.model_validate(document)
        except ValidationError as exc:
            reason, detail = _schema_reason(exc)
            return Invalid(reason, detail)

        out_of_range = _check_measurement_bounds(telemetry, settings)
        if out_of_range is not None:
            return out_of_range
        inconsistent = _check_power_consistency(telemetry, settings)
        if inconsistent is not None:
            return inconsistent
        return ValidTelemetry(topic=parsed, payload=telemetry)

    try:
        status = StatusPayloadV1.model_validate(document)
    except ValidationError as exc:
        reason, detail = _schema_reason(exc)
        return Invalid(reason, detail)
    return ValidStatus(topic=parsed, payload=status, retained=retained)
