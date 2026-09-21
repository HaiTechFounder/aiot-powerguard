"""MQTT v1 topic and payload validation."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from powerguard.config import Settings
from powerguard.mqtt.topics import TopicKind, parse_topic
from powerguard.mqtt.validation import (
    Invalid,
    RejectionReason,
    ValidStatus,
    ValidTelemetry,
    validate_message,
)
from tests.conftest import make_settings

DEVICE = "powerguard-01"
TELEMETRY_TOPIC = f"powerguard/v1/devices/{DEVICE}/telemetry"
STATUS_TOPIC = f"powerguard/v1/devices/{DEVICE}/status"


@pytest.fixture
def settings() -> Settings:
    return make_settings("sqlite:///./data/test.db")


def telemetry_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "boot_id": "7fa31c09",
        "seq": 42,
        "sampled_at": None,
        "voltage_v": 7.840,
        "current_a": 0.417,
        "power_w": 3.269,
        "energy_wh": 0.284,
        "sensor_status": "ok",
        "firmware_version": "0.1.0",
    }
    document.update(overrides)
    return document


def encode(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode("utf-8")


# --- topics ---------------------------------------------------------------


def test_topic_parsing_accepts_exactly_the_v1_shape() -> None:
    parsed = parse_topic(TELEMETRY_TOPIC)
    assert parsed is not None
    assert parsed.device_id == DEVICE
    assert parsed.kind is TopicKind.TELEMETRY
    assert parse_topic(STATUS_TOPIC).kind is TopicKind.STATUS  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "powerguard/v1/devices/powerguard-01",  # too few segments
        "powerguard/v1/devices/powerguard-01/telemetry/extra",  # too many
        "powerguard/v2/devices/powerguard-01/telemetry",  # wrong version
        "powerguard/v1/device/powerguard-01/telemetry",  # wrong namespace
        "powerguard/v1/devices/powerguard-01/command",  # unknown kind
        "powerguard/v1/devices/Powerguard-01/telemetry",  # uppercase device id
        "powerguard/v1/devices/-leading/telemetry",
        "powerguard/v1/devices//telemetry",  # empty device id
        "prefix/powerguard/v1/devices/powerguard-01/telemetry",
        "powerguard/v1/devices/" + "a" * 33 + "/telemetry",  # device id too long
    ],
)
def test_topic_parsing_rejects_anything_else(topic: str) -> None:
    assert parse_topic(topic) is None


def test_invalid_topic_is_rejected_before_the_payload(settings: Settings) -> None:
    result = validate_message("nonsense", b"not even json", settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.TOPIC_INVALID


# --- telemetry ------------------------------------------------------------


def test_valid_telemetry_is_accepted(settings: Settings) -> None:
    result = validate_message(TELEMETRY_TOPIC, encode(telemetry_document()), settings)
    assert isinstance(result, ValidTelemetry)
    assert result.topic.device_id == DEVICE
    assert result.payload.seq == 42
    assert result.payload.sampled_at is None
    assert result.payload.sampled_at_datetime() is None


def test_sampled_at_timestamp_is_parsed_when_present(settings: Settings) -> None:
    document = telemetry_document(sampled_at="2026-09-21T01:02:03.250Z")
    result = validate_message(TELEMETRY_TOPIC, encode(document), settings)
    assert isinstance(result, ValidTelemetry)
    moment = result.payload.sampled_at_datetime()
    assert moment is not None
    assert moment.isoformat() == "2026-09-21T01:02:03.250000+00:00"


def test_oversized_payload_is_rejected_before_parsing(settings: Settings) -> None:
    payload = b"{" + b"x" * (settings.max_payload_bytes + 1)
    result = validate_message(TELEMETRY_TOPIC, payload, settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.PAYLOAD_TOO_LARGE
    assert "limit=" in result.detail


def test_invalid_utf8_and_json_are_distinguished(settings: Settings) -> None:
    bad_utf8 = validate_message(TELEMETRY_TOPIC, b"\xff\xfe", settings)
    assert isinstance(bad_utf8, Invalid)
    assert bad_utf8.reason is RejectionReason.UTF8_INVALID

    bad_json = validate_message(TELEMETRY_TOPIC, b"{not json}", settings)
    assert isinstance(bad_json, Invalid)
    assert bad_json.reason is RejectionReason.JSON_INVALID

    not_an_object = validate_message(TELEMETRY_TOPIC, b"[1,2,3]", settings)
    assert isinstance(not_an_object, Invalid)
    assert not_an_object.reason is RejectionReason.SCHEMA_INVALID


def test_unsupported_schema_version_has_its_own_category(settings: Settings) -> None:
    document = telemetry_document(schema_version=2)
    result = validate_message(TELEMETRY_TOPIC, encode(document), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.VERSION_UNSUPPORTED


@pytest.mark.parametrize(
    "overrides",
    [
        {"boot_id": "7FA31C09"},  # uppercase hex
        {"boot_id": "7fa31c0"},  # too short
        {"seq": -1},
        {"seq": 4_294_967_296},
        {"seq": True},  # JSON boolean must not pass as an integer
        {"voltage_v": True},
        {"energy_wh": -0.001},
        {"sensor_status": "degraded"},
        {"firmware_version": "1.0"},
        {"firmware_version": "a" * 33},
        {"sampled_at": "2026-09-21T01:02:03Z"},  # no milliseconds
        {"sampled_at": 1758416400},
        {"voltage_v": "7.84"},  # string is not coerced
    ],
)
def test_schema_violations_are_rejected(settings: Settings, overrides: dict[str, Any]) -> None:
    result = validate_message(TELEMETRY_TOPIC, encode(telemetry_document(**overrides)), settings)
    assert isinstance(result, Invalid)
    assert result.reason in {
        RejectionReason.SCHEMA_INVALID,
        RejectionReason.MEASUREMENT_OUT_OF_RANGE,
    }


def test_unknown_and_missing_fields_are_rejected(settings: Settings) -> None:
    extra = telemetry_document()
    extra["device_id"] = DEVICE  # the payload must never carry the device id
    result = validate_message(TELEMETRY_TOPIC, encode(extra), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.SCHEMA_INVALID

    missing = telemetry_document()
    del missing["energy_wh"]
    result = validate_message(TELEMETRY_TOPIC, encode(missing), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.SCHEMA_INVALID


def test_non_finite_numbers_are_rejected(settings: Settings) -> None:
    # json.dumps emits bare NaN/Infinity, which json.loads accepts by default.
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = json.dumps(telemetry_document()).replace("7.84", literal)
        result = validate_message(TELEMETRY_TOPIC, raw.encode("utf-8"), settings)
        assert isinstance(result, Invalid), literal


def test_measurement_bounds_use_configured_limits(settings: Settings) -> None:
    too_high = validate_message(
        TELEMETRY_TOPIC, encode(telemetry_document(voltage_v=9.0, power_w=3.753)), settings
    )
    assert isinstance(too_high, Invalid)
    assert too_high.reason is RejectionReason.MEASUREMENT_OUT_OF_RANGE
    assert too_high.detail == "voltage_v"

    over_current = telemetry_document(current_a=9.0, power_w=70.56)
    result = validate_message(TELEMETRY_TOPIC, encode(over_current), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.MEASUREMENT_OUT_OF_RANGE


def test_reverse_flow_is_accepted(settings: Settings) -> None:
    # Negative current and power are legitimate readings, not errors.
    document = telemetry_document(current_a=-0.417, power_w=-3.269)
    assert isinstance(validate_message(TELEMETRY_TOPIC, encode(document), settings), ValidTelemetry)


def test_power_inconsistency_is_its_own_category(settings: Settings) -> None:
    document = telemetry_document(power_w=30.0)  # V x I is about 3.27 W
    result = validate_message(TELEMETRY_TOPIC, encode(document), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.POWER_INCONSISTENT


def test_power_consistency_check_can_be_disabled() -> None:
    relaxed = make_settings("sqlite:///./data/test.db", power_rel_tolerance=0.0)
    document = telemetry_document(power_w=30.0)
    assert isinstance(validate_message(TELEMETRY_TOPIC, encode(document), relaxed), ValidTelemetry)


def test_rejection_never_echoes_payload_content(settings: Settings) -> None:
    secret = "CORRELATION-SECRET-9f3a"
    document = telemetry_document(firmware_version=secret)
    result = validate_message(TELEMETRY_TOPIC, encode(document), settings)
    assert isinstance(result, Invalid)
    assert secret not in result.detail
    assert secret not in repr(result)


# --- status ---------------------------------------------------------------


def test_valid_status_is_accepted_with_retained_metadata(settings: Settings) -> None:
    document = {
        "schema_version": 1,
        "status": "online",
        "boot_id": "7fa31c09",
        "firmware_version": "0.1.0",
    }
    result = validate_message(STATUS_TOPIC, encode(document), settings, retained=True)
    assert isinstance(result, ValidStatus)
    assert result.payload.status == "online"
    # The retained flag is diagnostic metadata; the payload itself is untouched.
    assert result.retained is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "stale"},  # backend-owned, never device-reported
        {"status": "ONLINE"},
        {"boot_id": "zzzzzzzz"},
        {"firmware_version": "v1.0.0"},
    ],
)
def test_invalid_status_payloads_are_rejected(
    settings: Settings, overrides: dict[str, Any]
) -> None:
    document = {
        "schema_version": 1,
        "status": "online",
        "boot_id": "7fa31c09",
        "firmware_version": "0.1.0",
    }
    document.update(overrides)
    result = validate_message(STATUS_TOPIC, encode(document), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.SCHEMA_INVALID


def test_status_rejects_telemetry_fields(settings: Settings) -> None:
    result = validate_message(STATUS_TOPIC, encode(telemetry_document()), settings)
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.SCHEMA_INVALID


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-02-31T00:00:00.000Z",  # February has no 31st
        "2026-13-01T00:00:00.000Z",  # month 13
        "2026-00-10T00:00:00.000Z",  # month 0
        "2025-02-29T00:00:00.000Z",  # 2025 is not a leap year
        "2026-09-21T24:00:00.000Z",  # hour 24
        "2026-09-21T00:60:00.000Z",  # minute 60
    ],
)
def test_impossible_sampled_at_dates_are_rejected_deterministically(
    settings: Settings, stamp: str
) -> None:
    """The grammar matches, but the value is not a date.

    Such a payload must be rejected and acknowledged, never accepted and then
    raised on during ingestion.
    """
    result = validate_message(
        TELEMETRY_TOPIC, encode(telemetry_document(sampled_at=stamp)), settings
    )
    assert isinstance(result, Invalid)
    assert result.reason is RejectionReason.SCHEMA_INVALID
    assert result.detail == "sampled_at"
    assert stamp not in result.detail


def test_a_real_leap_day_is_accepted(settings: Settings) -> None:
    result = validate_message(
        TELEMETRY_TOPIC,
        encode(telemetry_document(sampled_at="2028-02-29T12:00:00.500Z")),
        settings,
    )
    assert isinstance(result, ValidTelemetry)
    parsed = result.payload.sampled_at_datetime()
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day, parsed.microsecond) == (2028, 2, 29, 500000)
    assert parsed.tzinfo is dt.UTC
