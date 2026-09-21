"""Initial Phase 01 schema: devices, telemetry, anomalies.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMP_LENGTH = 27


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("firmware_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_seen_at", sa.String(TIMESTAMP_LENGTH), nullable=False),
        sa.Column("last_seen_at", sa.String(TIMESTAMP_LENGTH), nullable=False),
        sa.Column("last_boot_id", sa.String(8), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
        sa.CheckConstraint("length(id) BETWEEN 1 AND 32", name="ck_devices_device_id_length"),
        sa.CheckConstraint(
            "length(firmware_version) BETWEEN 1 AND 32",
            name="ck_devices_firmware_version_length",
        ),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'stale')", name="ck_devices_status_allowed"
        ),
        sa.CheckConstraint(
            "last_boot_id IS NULL OR length(last_boot_id) = 8",
            name="ck_devices_last_boot_id_length",
        ),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="ck_devices_seen_order"),
    )

    op.create_table(
        "telemetry",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(32), nullable=False),
        sa.Column("boot_id", sa.String(8), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sampled_at", sa.String(TIMESTAMP_LENGTH), nullable=True),
        sa.Column("received_at", sa.String(TIMESTAMP_LENGTH), nullable=False),
        sa.Column("voltage_v", sa.Float(), nullable=False),
        sa.Column("current_a", sa.Float(), nullable=False),
        sa.Column("power_w", sa.Float(), nullable=False),
        sa.Column("energy_wh", sa.Float(), nullable=False),
        sa.Column("sensor_status", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_telemetry"),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name="fk_telemetry_device_id_devices",
            ondelete="CASCADE",
        ),
        # ADR-006 idempotency key for at-least-once QoS 1 delivery.
        sa.UniqueConstraint("device_id", "boot_id", "seq", name="uq_telemetry_device_boot_seq"),
        sa.CheckConstraint("seq BETWEEN 0 AND 4294967295", name="ck_telemetry_seq_range"),
        sa.CheckConstraint("length(boot_id) = 8", name="ck_telemetry_boot_id_length"),
        sa.CheckConstraint("sensor_status = 'ok'", name="ck_telemetry_sensor_status_v1"),
        sa.CheckConstraint("energy_wh >= 0", name="ck_telemetry_energy_non_negative"),
        # `x = x` is false for NaN, so these reject non-finite storage.
        sa.CheckConstraint("voltage_v = voltage_v", name="ck_telemetry_voltage_finite"),
        sa.CheckConstraint("current_a = current_a", name="ck_telemetry_current_finite"),
        sa.CheckConstraint("power_w = power_w", name="ck_telemetry_power_finite"),
        sa.CheckConstraint("energy_wh = energy_wh", name="ck_telemetry_energy_finite"),
    )
    op.execute(
        "CREATE INDEX telemetry_device_received "
        "ON telemetry (device_id, received_at DESC, id DESC)"
    )

    op.create_table(
        "anomalies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telemetry_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.String(32), nullable=False),
        sa.Column("detected_at", sa.String(TIMESTAMP_LENGTH), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_anomalies"),
        sa.ForeignKeyConstraint(
            ["telemetry_id"],
            ["telemetry.id"],
            name="fk_anomalies_telemetry_id_telemetry",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name="fk_anomalies_device_id_devices",
            ondelete="CASCADE",
        ),
        # One anomaly per telemetry row.
        sa.UniqueConstraint("telemetry_id", name="uq_anomalies_telemetry_id"),
        sa.CheckConstraint(
            "method IN ('rule', 'isolation_forest', 'hybrid')",
            name="ck_anomalies_method_allowed",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1 AND score = score)",
            name="ck_anomalies_score_range",
        ),
        sa.CheckConstraint(
            "model_version IS NULL OR length(model_version) <= 128",
            name="ck_anomalies_model_version_length",
        ),
        sa.CheckConstraint("length(reasons_json) > 0", name="ck_anomalies_reasons_present"),
    )
    op.execute(
        "CREATE INDEX anomalies_device_detected "
        "ON anomalies (device_id, detected_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS anomalies_device_detected")
    op.drop_table("anomalies")
    op.execute("DROP INDEX IF EXISTS telemetry_device_received")
    op.drop_table("telemetry")
    op.drop_table("devices")
