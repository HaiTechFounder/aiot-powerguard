"""Typed application settings.

One immutable settings root, loaded from the ``POWERGUARD_`` environment prefix
and an optional local ``.env``. Secrets use ``SecretStr`` so they never reach a
repr, a log line, an error message or the health endpoint.

Importing this module has no side effects: nothing reads the environment until
:func:`get_settings` or :class:`Settings` is constructed, so tests can inject
their own instance.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["development", "test", "production"]
LogFormat = Literal["console", "json"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Repository-local default for the runtime database file (git-ignored).
_DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "powerguard.db"

# ADR-003: SQLite only. The driver suffix is optional; anything else is refused
# at startup rather than failing later inside SQLAlchemy.
_SQLITE_URL = re.compile(r"^sqlite(?:\+(?P<driver>[a-z0-9_]+))?://(?P<rest>.*)$")
_SUPPORTED_SQLITE_DRIVERS = frozenset({"pysqlite"})


class DatabaseUrlError(ValueError):
    """The configured DATABASE_URL is not a SQLite URL this build can open."""


def parse_sqlite_url(value: str) -> Path | None:
    """Validate a SQLite URL and return its file path, or None for in-memory.

    Raises :class:`DatabaseUrlError` for anything malformed, so a typo fails at
    startup instead of silently creating a database in an unexpected place.
    """
    match = _SQLITE_URL.match(value)
    if match is None:
        raise DatabaseUrlError("DATABASE_URL must be a SQLite URL (ADR-003)")
    driver = match.group("driver")
    if driver is not None and driver not in _SUPPORTED_SQLITE_DRIVERS:
        raise DatabaseUrlError(f"unsupported SQLite driver: {driver!r}")

    rest = match.group("rest")
    if rest == "":
        # `sqlite://` is SQLAlchemy's anonymous in-memory database.
        return None
    if not rest.startswith("/"):
        # `sqlite://host/db` — a network location is meaningless for SQLite and
        # would silently be read as a relative path.
        raise DatabaseUrlError("SQLite URL must not contain a host component")

    tail = rest[1:]
    if tail == "":
        raise DatabaseUrlError("SQLite URL has no database path")
    if tail == ":memory:":
        return None
    if tail.startswith("file:"):
        raise DatabaseUrlError("SQLite URI filenames are not supported")
    if chr(0) in tail:
        raise DatabaseUrlError("SQLite path contains a null byte")
    if "?" in tail:
        raise DatabaseUrlError("SQLite query parameters are not supported")
    if tail.endswith(("/", "\\")):
        raise DatabaseUrlError("SQLite path must name a file, not a directory")

    path = Path(tail)
    if path.is_dir():
        raise DatabaseUrlError("SQLite path is an existing directory")
    return path


def _require_finite(value: float, field: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field.upper()} must be a finite number")
    return value


class Settings(BaseSettings):
    """Validated configuration for one backend process."""

    model_config = SettingsConfigDict(
        env_prefix="POWERGUARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    app_env: AppEnv = "development"

    # --- HTTP -------------------------------------------------------------
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=1, le=65535)
    # Exact allow-list. "*" is rejected: credentials may be sent from the UI.
    # NoDecode: the value is a comma-separated list, not JSON.
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ("http://127.0.0.1:5173",)

    # --- Database ---------------------------------------------------------
    database_url: str = f"sqlite:///{_DEFAULT_DB_PATH.as_posix()}"
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=100, le=120_000)

    # --- MQTT -------------------------------------------------------------
    # May be disabled for API-only development and for tests that inject a fake
    # client. When enabled, broker credentials are mandatory (MQTT_SPEC: the
    # broker has anonymous access disabled).
    mqtt_enabled: bool = True
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_instance_id: str = "local"
    mqtt_keepalive_s: int = Field(default=30, ge=5, le=3600)
    mqtt_ingress_queue_size: int = Field(default=1000, ge=1, le=100_000)
    mqtt_shutdown_grace_s: float = Field(default=10.0, gt=0.0, le=120.0)

    # --- Device status ----------------------------------------------------
    stale_after_s: float = Field(default=15.0, gt=0.0, le=3600.0)
    stale_scan_interval_s: float = Field(default=2.0, gt=0.0, le=3600.0)

    # --- WebSocket --------------------------------------------------------
    ws_max_connections: int = Field(default=100, ge=1, le=10_000)
    ws_queue_size: int = Field(default=64, ge=1, le=10_000)
    ws_ping_interval_s: float = Field(default=20.0, gt=0.0, le=600.0)
    ws_ping_timeout_s: float = Field(default=20.0, gt=0.0, le=600.0)

    # --- Logging ----------------------------------------------------------
    log_format: LogFormat = "console"
    log_level: LogLevel = "INFO"

    # --- Ingestion bounds -------------------------------------------------
    # Protocol-integrity bounds, NOT safety thresholds. Warning and overcurrent
    # policy is a separate concern and is not configured here.
    max_payload_bytes: int = Field(default=2048, ge=64, le=65_536)
    min_voltage_v: float = 0.0
    max_voltage_v: float = 8.4
    # HARDWARE_CONFIGURATION_PENDING: development values until the hardware
    # measurement bounds are validated. They must not be relabelled as safe
    # operating limits.
    max_abs_current_a: float = Field(default=8.19, gt=0.0)
    max_abs_power_w: float = Field(default=68.8, gt=0.0)
    # Optional consistency check between power_w and voltage_v * current_a.
    power_rel_tolerance: float = Field(default=0.05, ge=0.0, le=1.0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard_origin(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("CORS_ORIGINS must list at least one exact origin")
        for origin in value:
            if origin == "*":
                raise ValueError("CORS_ORIGINS must be an exact allow-list, not '*'")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS origin must include a scheme: {origin!r}")
        return value

    @field_validator("database_url")
    @classmethod
    def _require_supported_database(cls, value: str) -> str:
        # Parsed eagerly: a malformed URL must fail here, not on first query.
        parse_sqlite_url(value)
        return value

    @field_validator(
        "min_voltage_v",
        "max_voltage_v",
        "max_abs_current_a",
        "max_abs_power_w",
        "power_rel_tolerance",
        "stale_after_s",
        "stale_scan_interval_s",
        "mqtt_shutdown_grace_s",
        "ws_ping_interval_s",
        "ws_ping_timeout_s",
    )
    @classmethod
    def _require_finite_bound(cls, value: float, info: ValidationInfo) -> float:
        # Pydantic accepts inf and nan as floats; an infinite measurement bound
        # would disable the ingestion range check entirely.
        return _require_finite(value, info.field_name or "value")

    @field_validator("mqtt_username")
    @classmethod
    def _strip_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _check_cross_field_rules(self) -> Settings:
        if self.mqtt_enabled:
            # The message never contains the value itself.
            if self.mqtt_username is None:
                raise ValueError("MQTT_USERNAME is required when MQTT_ENABLED is true")
            if self.mqtt_password is None or not self.mqtt_password.get_secret_value():
                raise ValueError("MQTT_PASSWORD is required when MQTT_ENABLED is true")
        if self.max_voltage_v <= self.min_voltage_v:
            raise ValueError("MAX_VOLTAGE_V must be greater than MIN_VOLTAGE_V")
        if self.min_voltage_v < 0.0:
            raise ValueError("MIN_VOLTAGE_V must not be negative")
        if self.stale_scan_interval_s > self.stale_after_s:
            raise ValueError("STALE_SCAN_INTERVAL_S must not exceed STALE_AFTER_S")
        if self.ws_ping_timeout_s < self.ws_ping_interval_s:
            raise ValueError("WS_PING_TIMEOUT_S must be at least WS_PING_INTERVAL_S")
        return self

    # ------------------------------------------------------------------
    @property
    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite database, or None for in-memory."""
        return parse_sqlite_url(self.database_url)

    @property
    def is_sqlite_memory(self) -> bool:
        return self.sqlite_path is None

    def redacted(self) -> dict[str, object]:
        """Configuration snapshot safe to log or expose. Secrets are elided."""
        data = self.model_dump()
        data["mqtt_password"] = "<set>" if self.mqtt_password else "<missing>"
        data["mqtt_username"] = "<set>" if self.mqtt_username else "<missing>"
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read from the environment on first use."""
    return Settings()
