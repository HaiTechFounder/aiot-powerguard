"""Structured logging and redaction.

BACKEND_SPEC forbids payload bytes, credentials and whole settings objects from
reaching a log line. These tests capture what the configured handler actually
writes, so the guarantee is asserted on rendered output rather than on intent.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from powerguard.config import Settings
from powerguard.observability import (
    REDACTED,
    JsonFormatter,
    SecretScrubber,
    configure_logging,
    log_event,
    redact_fields,
    secrets_from_settings,
)
from tests.conftest import make_settings

SECRET = "s3cr3t-broker-password"


@pytest.fixture
def captured() -> io.StringIO:
    """Capture exactly what the configured handler emits."""
    return io.StringIO()


def install(
    stream: io.StringIO, *, log_format: str = "console", secrets: tuple[str, ...] = (SECRET,)
) -> logging.Logger:
    configure_logging("DEBUG", log_format, secrets=secrets)
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_powerguard_handler", False):
            handler.setStream(stream)  # type: ignore[attr-defined]
    return logging.getLogger("powerguard.test")


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    root = logging.getLogger()
    before = list(root.handlers), root.level
    yield
    root.handlers = before[0]
    root.setLevel(before[1])


# -- field redaction -------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["password", "mqtt_password", "Secret", "api_token", "credential", "authorization"],
)
def test_secret_looking_fields_are_never_rendered(field: str) -> None:
    assert redact_fields({field: SECRET}) == {field: REDACTED}


def test_payload_bytes_become_a_length_not_content() -> None:
    clean = redact_fields({"payload": b'{"voltage_v": 7.84}'})
    assert clean == {"payload": "<19 bytes>"}


def test_any_bytes_field_is_summarised() -> None:
    assert redact_fields({"blob": b"abcd"}) == {"blob": "<4 bytes>"}


def test_correlation_fields_survive_untouched() -> None:
    clean = redact_fields({"device_id": "powerguard-01", "seq": 42, "mid": 7})
    assert clean == {"device_id": "powerguard-01", "seq": 42, "mid": 7}


# -- rendered output -------------------------------------------------------


def test_event_line_carries_the_event_name_and_fields(captured: io.StringIO) -> None:
    logger = install(captured)
    log_event(logger, logging.INFO, "telemetry_accepted", device_id="powerguard-01", seq=9)

    line = captured.getvalue()
    assert "telemetry_accepted device_id=powerguard-01 seq=9" in line
    assert "INFO" in line


def test_json_format_emits_one_object_per_record(captured: io.StringIO) -> None:
    logger = install(captured, log_format="json")
    log_event(logger, logging.WARNING, "mqtt_disconnected", host="127.0.0.1")

    record = json.loads(captured.getvalue().strip())
    assert record["event"] == "mqtt_disconnected"
    assert record["level"] == "WARNING"
    assert record["fields"] == {"host": "127.0.0.1"}
    assert record["ts"].endswith("+00:00"), "timestamps are UTC, never local"


def test_a_payload_passed_as_a_field_never_reaches_the_output(
    captured: io.StringIO,
) -> None:
    logger = install(captured)
    log_event(logger, logging.ERROR, "rejected", payload=b'{"voltage_v": 7.84}')

    output = captured.getvalue()
    assert "voltage_v" not in output
    assert "<19 bytes>" in output


# -- the second guard: literal secret scrubbing ----------------------------


def test_a_secret_logged_by_careless_code_is_still_scrubbed(
    captured: io.StringIO,
) -> None:
    logger = install(captured)
    # Not log_event, and not even this project's format: a third-party library
    # logging a connection string would look like this.
    logger.warning("connecting with password %s", SECRET)

    output = captured.getvalue()
    assert SECRET not in output
    assert REDACTED in output


def test_scrubbing_survives_percent_formatting_and_exceptions(
    captured: io.StringIO,
) -> None:
    logger = install(captured)
    try:
        raise RuntimeError(f"auth failed for {SECRET}")
    except RuntimeError:
        logger.exception("mqtt_connect_failed")

    assert SECRET not in captured.getvalue()


def test_no_configured_secret_leaves_records_untouched() -> None:
    scrubber = SecretScrubber([])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "unchanged %s", ("x",), None)
    assert scrubber.filter(record) is True
    assert record.getMessage() == "unchanged x"


def test_secrets_are_collected_from_settings() -> None:
    settings: Settings = make_settings(
        "sqlite:///./x.db",
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password=SECRET,
    )
    assert secrets_from_settings(settings) == (SECRET,)
    # The settings object itself still never exposes it.
    assert SECRET not in repr(settings)
    assert settings.redacted()["mqtt_password"] == "<set>"


def test_settings_without_a_password_yield_no_secrets() -> None:
    assert secrets_from_settings(make_settings("sqlite:///./x.db")) == ()


def test_json_formatter_falls_back_when_a_record_has_no_event() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "plain message", None, None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "plain"
    assert payload["message"] == "plain message"
