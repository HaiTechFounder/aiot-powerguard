"""The migrated schema matches the approved Phase 01 design."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, event, inspect, text

from powerguard.config import Settings
from powerguard.db.engine import (
    create_database_engine,
    journal_mode,
    pragma_value,
    sqlite_pragmas,
)
from tests.conftest import alembic_config, make_settings


def test_tables_and_columns(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    assert {"devices", "telemetry", "anomalies"} <= set(inspector.get_table_names())

    telemetry_columns = {c["name"] for c in inspector.get_columns("telemetry")}
    assert telemetry_columns == {
        "id",
        "device_id",
        "boot_id",
        "seq",
        "sampled_at",
        "received_at",
        "voltage_v",
        "current_a",
        "power_w",
        "energy_wh",
        "sensor_status",
    }


def test_idempotency_unique_key(migrated_engine: Engine) -> None:
    constraints = inspect(migrated_engine).get_unique_constraints("telemetry")
    by_name = {c["name"]: c["column_names"] for c in constraints}
    assert by_name["uq_telemetry_device_boot_seq"] == ["device_id", "boot_id", "seq"]


def test_descending_query_indexes_exist(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        ).all()
    sql_by_name = dict(rows)

    telemetry_sql = sql_by_name["telemetry_device_received"]
    assert "device_id" in telemetry_sql
    assert "received_at DESC" in telemetry_sql
    assert "id DESC" in telemetry_sql

    anomaly_sql = sql_by_name["anomalies_device_detected"]
    assert "device_id" in anomaly_sql
    assert "detected_at DESC" in anomaly_sql
    assert "id DESC" in anomaly_sql


def test_one_anomaly_per_telemetry_row(migrated_engine: Engine) -> None:
    constraints = inspect(migrated_engine).get_unique_constraints("anomalies")
    assert any(c["column_names"] == ["telemetry_id"] for c in constraints)


def test_foreign_keys_cascade(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    telemetry_fks = inspector.get_foreign_keys("telemetry")
    assert telemetry_fks[0]["referred_table"] == "devices"
    assert telemetry_fks[0]["options"]["ondelete"] == "CASCADE"

    referred = {fk["referred_table"] for fk in inspector.get_foreign_keys("anomalies")}
    assert referred == {"telemetry", "devices"}


@pytest.mark.parametrize(
    "statement",
    [
        # seq out of range
        "INSERT INTO telemetry (device_id, boot_id, seq, received_at, voltage_v, current_a,"
        " power_w, energy_wh, sensor_status) VALUES ('powerguard-01','7fa31c09',-1,"
        "'2026-09-21T01:00:00.000000Z',7.0,0.5,3.5,0.1,'ok')",
        # negative energy
        "INSERT INTO telemetry (device_id, boot_id, seq, received_at, voltage_v, current_a,"
        " power_w, energy_wh, sensor_status) VALUES ('powerguard-01','7fa31c09',1,"
        "'2026-09-21T01:00:00.000000Z',7.0,0.5,3.5,-0.1,'ok')",
        # sensor_status other than the v1 value
        "INSERT INTO telemetry (device_id, boot_id, seq, received_at, voltage_v, current_a,"
        " power_w, energy_wh, sensor_status) VALUES ('powerguard-01','7fa31c09',2,"
        "'2026-09-21T01:00:00.000000Z',7.0,0.5,3.5,0.1,'degraded')",
        # boot_id wrong length
        "INSERT INTO telemetry (device_id, boot_id, seq, received_at, voltage_v, current_a,"
        " power_w, energy_wh, sensor_status) VALUES ('powerguard-01','abc',3,"
        "'2026-09-21T01:00:00.000000Z',7.0,0.5,3.5,0.1,'ok')",
    ],
)
def test_check_constraints_reject_invalid_rows(migrated_engine: Engine, statement: str) -> None:
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO devices (id, firmware_version, status, first_seen_at, last_seen_at)"
                " VALUES ('powerguard-01','0.1.0','online','2026-09-21T01:00:00.000000Z',"
                "'2026-09-21T01:00:00.000000Z')"
            )
        )
    with (
        pytest.raises(Exception, match="CHECK constraint failed"),
        migrated_engine.begin() as connection,
    ):
        connection.execute(text(statement))


def test_connection_pragmas(migrated_engine: Engine) -> None:
    assert journal_mode(migrated_engine) == "wal"
    assert pragma_value(migrated_engine, "foreign_keys") == 1
    assert pragma_value(migrated_engine, "busy_timeout") == 5000


def test_in_memory_database_reports_its_real_journal_mode(tmp_path: Path) -> None:
    # WAL is unavailable in memory; assert what SQLite actually reports rather
    # than pretending the file behaviour applies.
    settings: Settings = make_settings("sqlite:///:memory:")
    engine = create_database_engine(settings)
    try:
        assert journal_mode(engine) == "memory"
        assert pragma_value(engine, "foreign_keys") == 1
    finally:
        engine.dispose()
    del tmp_path


def test_alembic_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'cycle.db').as_posix()}"
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = create_database_engine(make_settings(url))
    try:
        assert {"devices", "telemetry", "anomalies"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_second_upgrade_is_a_no_op(migrated_engine: Engine, settings: Settings) -> None:
    command.upgrade(alembic_config(settings.database_url), "head")
    with migrated_engine.connect() as connection:
        revisions = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    assert len(revisions) == 1


def test_alembic_applies_the_same_pragmas_as_the_application(tmp_path: Path) -> None:
    """A migration must not run under different locking or durability rules.

    The statements Alembic's own connection issues are captured, because the
    application engine would set WAL on connect anyway: reading the mode
    afterwards would prove nothing about the migration.
    """
    database = tmp_path / "migrated.db"
    settings = make_settings(f"sqlite:///{database.as_posix()}", sqlite_busy_timeout_ms=7000)

    statements: list[str] = []

    @event.listens_for(Engine, "before_cursor_execute")
    def _record(  # type: ignore[no-untyped-def]
        _conn, _cursor, statement, _params, _context, _executemany
    ) -> None:
        statements.append(str(statement))

    try:
        command.upgrade(
            alembic_config(settings.database_url, busy_timeout_ms="7000"), "head"
        )
    finally:
        event.remove(Engine, "before_cursor_execute", _record)

    pragmas = [line for line in statements if line.upper().startswith("PRAGMA")]
    assert "PRAGMA foreign_keys=ON" in pragmas
    assert "PRAGMA busy_timeout=7000" in pragmas
    assert "PRAGMA journal_mode=WAL" in pragmas

    # WAL is a persistent property of the file, so a connection that does not
    # request it still reports what the migration left behind.
    with sqlite3.connect(database) as raw:
        assert str(raw.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_pragma_list_is_the_single_source_of_truth() -> None:
    file_pragmas = sqlite_pragmas(5000, use_wal=True)
    assert "PRAGMA foreign_keys=ON" in file_pragmas
    assert "PRAGMA busy_timeout=5000" in file_pragmas
    assert "PRAGMA journal_mode=WAL" in file_pragmas

    # WAL is impossible in memory and is therefore never requested there.
    memory_pragmas = sqlite_pragmas(5000, use_wal=False)
    assert "PRAGMA journal_mode=WAL" not in memory_pragmas
    assert "PRAGMA foreign_keys=ON" in memory_pragmas
