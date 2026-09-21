"""Application factory, lifespan and CLI boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient

from powerguard.bootstrap import MigrationError, current_revision, verify_migrations
from powerguard.config import Settings
from powerguard.db.engine import create_database_engine
from powerguard.main import create_app
from tests.conftest import alembic_config, make_settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_create_app_does_not_touch_the_database_or_broker(settings: Settings) -> None:
    # No migration has run yet: building the app must still succeed, because
    # resources belong to the lifespan, not to import or construction.
    app = create_app(settings)
    assert app.state.settings is settings
    assert not hasattr(app.state, "container")


def test_health_is_served_and_leaks_no_configuration(settings: Settings) -> None:
    command.upgrade(alembic_config(settings.database_url), "head")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    # Exactly the API_CONTRACT health shape.
    assert set(body) == {"status", "database", "mqtt", "model", "version"}
    assert body["status"] == "ok"
    assert body["database"] == "ready"
    # MQTT and model readiness are reported independently of overall health.
    assert body["mqtt"] == "disconnected"
    assert body["model"] == "unavailable"
    assert settings.database_url not in response.text


def test_startup_refuses_an_unapplied_migration(settings: Settings) -> None:
    # Database file exists but was never migrated.
    with pytest.raises(MigrationError, match="upgrade head"), TestClient(create_app(settings)):
        pass


def test_verify_migrations_rejects_an_unexpected_revision(settings: Settings) -> None:
    command.upgrade(alembic_config(settings.database_url), "head")
    engine = create_database_engine(settings)
    try:
        assert current_revision(engine) == "0001_initial_schema"
        verify_migrations(engine)
        with pytest.raises(MigrationError, match="expects"):
            verify_migrations(engine, expected="9999_not_this_one")
    finally:
        engine.dispose()


def test_cors_allow_list_is_exact(settings: Settings) -> None:
    command.upgrade(alembic_config(settings.database_url), "head")
    with TestClient(create_app(settings)) as client:
        allowed = client.get(
            "/api/v1/health", headers={"Origin": "http://127.0.0.1:5173"}
        )
        rejected = client.get("/api/v1/health", headers={"Origin": "http://evil.example"})
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-origin" not in rejected.headers


def test_cli_help_works_without_configuration() -> None:
    # --help must not require a .env, a database or a broker.
    result = subprocess.run(
        [sys.executable, "-m", "powerguard", "--help"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows"},
    )
    assert result.returncode == 0
    assert "check-config" in result.stdout
    assert "check-db" in result.stdout


def test_check_config_reports_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from powerguard.__main__ import main

    monkeypatch.setenv("POWERGUARD_MQTT_ENABLED", "true")
    monkeypatch.delenv("POWERGUARD_MQTT_USERNAME", raising=False)
    monkeypatch.delenv("POWERGUARD_MQTT_PASSWORD", raising=False)
    monkeypatch.chdir(BACKEND_ROOT.parent)  # avoid picking up a local .env
    assert main(["check-config"]) == 2


def test_settings_injection_keeps_processes_independent(database_url: str) -> None:
    first = make_settings(database_url, http_port=8001)
    second = make_settings(database_url, http_port=8002)
    assert create_app(first).state.settings.http_port == 8001
    assert create_app(second).state.settings.http_port == 8002


def test_serve_passes_the_configured_websocket_ping_settings(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    """A configured liveness interval that never reaches the server is decorative."""
    captured: dict[str, object] = {}

    def fake_run(_app: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("POWERGUARD_DATABASE_URL", database_url)
    monkeypatch.setenv("POWERGUARD_MQTT_ENABLED", "false")
    monkeypatch.setenv("POWERGUARD_WS_PING_INTERVAL_S", "7.5")
    monkeypatch.setenv("POWERGUARD_WS_PING_TIMEOUT_S", "11.5")
    monkeypatch.setenv("POWERGUARD_HTTP_PORT", "8123")
    monkeypatch.setattr("uvicorn.run", fake_run)

    from powerguard.__main__ import main

    assert main(["serve"]) == 0
    assert captured["ws_ping_interval"] == pytest.approx(7.5)
    assert captured["ws_ping_timeout"] == pytest.approx(11.5)
    assert captured["port"] == 8123


def test_serve_installs_logging_that_hides_the_broker_password(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    installed: dict[str, object] = {}

    def fake_configure(level: str, fmt: str, *, secrets: tuple[str, ...]) -> None:
        installed.update({"level": level, "format": fmt, "secrets": secrets})

    monkeypatch.setenv("POWERGUARD_DATABASE_URL", database_url)
    monkeypatch.setenv("POWERGUARD_MQTT_USERNAME", "backend")
    monkeypatch.setenv("POWERGUARD_MQTT_PASSWORD", "top-secret")
    monkeypatch.setenv("POWERGUARD_LOG_FORMAT", "json")
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: None)
    monkeypatch.setattr("powerguard.observability.configure_logging", fake_configure)

    from powerguard.__main__ import main

    assert main(["serve"]) == 0
    assert installed["format"] == "json"
    assert installed["secrets"] == ("top-secret",)


def test_check_db_reports_the_real_sqlite_state(
    monkeypatch: pytest.MonkeyPatch, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    command.upgrade(alembic_config(database_url), "head")
    monkeypatch.setenv("POWERGUARD_DATABASE_URL", database_url)
    monkeypatch.setenv("POWERGUARD_MQTT_ENABLED", "false")

    from powerguard.__main__ import main

    assert main(["check-db"]) == 0
    printed = dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line
    )
    assert printed["journal_mode   "].strip() == "wal"
    assert printed["foreign_keys   "].strip() == "1"
    assert printed["alembic_head   "].strip() == "0001_initial_schema"


def test_check_config_prints_a_redacted_summary(
    monkeypatch: pytest.MonkeyPatch, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERGUARD_DATABASE_URL", database_url)
    monkeypatch.setenv("POWERGUARD_MQTT_USERNAME", "backend")
    monkeypatch.setenv("POWERGUARD_MQTT_PASSWORD", "must-not-print")

    from powerguard.__main__ import main

    assert main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "must-not-print" not in out
    assert "mqtt_password = <set>" in out
