"""Configuration validation and secret redaction."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from powerguard.config import Settings
from tests.conftest import make_settings

SECRET = "super-secret-value"


def test_defaults_load_with_mqtt_disabled() -> None:
    settings = make_settings("sqlite:///./data/test.db")
    assert settings.http_host == "127.0.0.1"
    assert settings.max_payload_bytes == 2048
    assert settings.mqtt_enabled is False


def test_mqtt_requires_username_and_password_when_enabled() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(mqtt_enabled=True, mqtt_username=None, mqtt_password=None)
    assert "MQTT_USERNAME" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(mqtt_enabled=True, mqtt_username="backend", mqtt_password=None)
    message = str(excinfo.value)
    assert "MQTT_PASSWORD" in message
    assert SECRET not in message


def test_secret_never_appears_in_repr_or_errors() -> None:
    settings = Settings(
        mqtt_enabled=True, mqtt_username="backend", mqtt_password=SECRET
    )
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.model_dump())
    redacted = settings.redacted()
    assert redacted["mqtt_password"] == "<set>"
    assert redacted["mqtt_username"] == "<set>"
    assert SECRET not in str(redacted)
    # The value is still reachable for the one component that needs it.
    assert settings.mqtt_password is not None
    assert settings.mqtt_password.get_secret_value() == SECRET


def test_environment_overrides_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWERGUARD_HTTP_PORT", "9123")
    monkeypatch.setenv("POWERGUARD_MQTT_ENABLED", "false")
    monkeypatch.setenv("POWERGUARD_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    settings = Settings()
    assert settings.http_port == 9123
    assert settings.cors_origins == ("http://127.0.0.1:5173", "http://localhost:5173")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("http_port", 0),
        ("http_port", 70000),
        ("sqlite_busy_timeout_ms", 0),
        ("mqtt_ingress_queue_size", 0),
        ("ws_queue_size", 0),
        ("max_payload_bytes", 8),
        ("max_abs_current_a", 0.0),
        ("power_rel_tolerance", 1.5),
        ("stale_after_s", 0.0),
    ],
)
def test_out_of_range_values_fail_fast(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings("sqlite:///./data/test.db", **{field: value})


def test_cors_rejects_wildcard_and_schemeless_origins() -> None:
    with pytest.raises(ValidationError, match="exact allow-list"):
        make_settings("sqlite:///./data/test.db", cors_origins="*")
    with pytest.raises(ValidationError, match="scheme"):
        make_settings("sqlite:///./data/test.db", cors_origins="127.0.0.1:5173")


def test_cross_field_rules() -> None:
    with pytest.raises(ValidationError, match="MAX_VOLTAGE_V"):
        make_settings("sqlite:///./data/test.db", min_voltage_v=9.0, max_voltage_v=8.4)
    with pytest.raises(ValidationError, match="STALE_SCAN_INTERVAL_S"):
        make_settings("sqlite:///./data/test.db", stale_after_s=1.0, stale_scan_interval_s=5.0)
    with pytest.raises(ValidationError, match="WS_PING_TIMEOUT_S"):
        make_settings("sqlite:///./data/test.db", ws_ping_interval_s=30.0, ws_ping_timeout_s=5.0)


def test_non_sqlite_database_is_rejected() -> None:
    with pytest.raises(ValidationError, match="SQLite"):
        make_settings("postgresql://localhost/powerguard")


def test_unknown_setting_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings("sqlite:///./data/test.db", not_a_real_setting=1)


@pytest.mark.parametrize(
    "field",
    [
        "min_voltage_v",
        "max_voltage_v",
        "max_abs_current_a",
        "max_abs_power_w",
        "power_rel_tolerance",
        "stale_after_s",
        "mqtt_shutdown_grace_s",
        "ws_ping_interval_s",
    ],
)
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_bounds_are_rejected(field: str, value: float) -> None:
    """An infinite bound would silently disable the ingestion range check."""
    with pytest.raises(ValidationError):
        make_settings("sqlite:///./x.db", **{field: value})


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///",
        "sqlite://host/powerguard.db",
        "sqlite+aiosqlite:///./powerguard.db",
        "sqlite:///./powerguard.db?mode=ro",
        "sqlite:///file:memdb?mode=memory",
        "sqlite:///data/",
        "postgresql://localhost/powerguard",
        "not a url",
        "",
    ],
)
def test_malformed_sqlite_urls_fail_fast(url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(url)


@pytest.mark.parametrize(
    ("url", "is_memory"),
    [
        ("sqlite://", True),
        ("sqlite:///:memory:", True),
        ("sqlite+pysqlite:///:memory:", True),
        ("sqlite:///./powerguard.db", False),
        ("sqlite:///D:/data/powerguard.db", False),
    ],
)
def test_supported_sqlite_urls_resolve_their_path(url: str, is_memory: bool) -> None:
    settings = make_settings(url)
    assert settings.is_sqlite_memory is is_memory
    assert (settings.sqlite_path is None) is is_memory


def test_existing_directory_is_not_a_database(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(f"sqlite:///{tmp_path.as_posix()}")
