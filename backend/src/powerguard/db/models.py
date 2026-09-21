"""ORM models implementing the Phase 01 database schema exactly.

These rows never leave the database adapter: repositories map them to domain
entities. Alembic owns schema creation; nothing here calls ``create_all``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from powerguard.db.base import Base
from powerguard.db.types import UtcTimestamp

DEVICE_ID_MAX = 32
FIRMWARE_VERSION_MAX = 32
BOOT_ID_LENGTH = 8
SEQ_MAX = 4_294_967_295
MODEL_VERSION_MAX = 128

DEVICE_STATUSES = ("online", "offline", "stale")
ANOMALY_METHODS = ("rule", "isolation_forest", "hybrid")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(DEVICE_ID_MAX), primary_key=True)
    firmware_version: Mapped[str] = mapped_column(String(FIRMWARE_VERSION_MAX), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(UtcTimestamp, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UtcTimestamp, nullable=False)
    last_boot_id: Mapped[str | None] = mapped_column(String(BOOT_ID_LENGTH), nullable=True)

    telemetry: Mapped[list[Telemetry]] = relationship(back_populates="device")

    __table_args__ = (
        CheckConstraint(
            f"length(id) BETWEEN 1 AND {DEVICE_ID_MAX}",
            name="device_id_length",
        ),
        CheckConstraint(
            f"length(firmware_version) BETWEEN 1 AND {FIRMWARE_VERSION_MAX}",
            name="firmware_version_length",
        ),
        CheckConstraint(_in_list("status", DEVICE_STATUSES), name="status_allowed"),
        CheckConstraint(
            f"last_boot_id IS NULL OR length(last_boot_id) = {BOOT_ID_LENGTH}",
            name="last_boot_id_length",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
    )


class Telemetry(Base):
    __tablename__ = "telemetry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(DEVICE_ID_MAX),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    boot_id: Mapped[str] = mapped_column(String(BOOT_ID_LENGTH), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    sampled_at: Mapped[dt.datetime | None] = mapped_column(UtcTimestamp, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(UtcTimestamp, nullable=False)
    voltage_v: Mapped[float] = mapped_column(Float, nullable=False)
    current_a: Mapped[float] = mapped_column(Float, nullable=False)
    power_w: Mapped[float] = mapped_column(Float, nullable=False)
    energy_wh: Mapped[float] = mapped_column(Float, nullable=False)
    sensor_status: Mapped[str] = mapped_column(String(16), nullable=False)

    device: Mapped[Device] = relationship(back_populates="telemetry")

    __table_args__ = (
        # QoS 1 idempotency key (ADR-006). The database is the authority: the
        # ingestion path relies on this constraint, not on a pre-insert query.
        UniqueConstraint("device_id", "boot_id", "seq", name="uq_telemetry_device_boot_seq"),
        CheckConstraint(f"seq BETWEEN 0 AND {SEQ_MAX}", name="seq_range"),
        CheckConstraint(f"length(boot_id) = {BOOT_ID_LENGTH}", name="boot_id_length"),
        CheckConstraint("sensor_status = 'ok'", name="sensor_status_v1"),
        CheckConstraint("energy_wh >= 0", name="energy_non_negative"),
        # SQLite has no IS FINITE; comparing a value to itself rejects NaN, and
        # the explicit bounds reject the infinities.
        CheckConstraint("voltage_v = voltage_v", name="voltage_finite"),
        CheckConstraint("current_a = current_a", name="current_finite"),
        CheckConstraint("power_w = power_w", name="power_finite"),
        CheckConstraint("energy_wh = energy_wh", name="energy_finite"),
        Index(
            "telemetry_device_received",
            "device_id",
            text("received_at DESC"),
            text("id DESC"),
        ),
    )


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telemetry_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("telemetry.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    device_id: Mapped[str] = mapped_column(
        String(DEVICE_ID_MAX),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    detected_at: Mapped[dt.datetime] = mapped_column(UtcTimestamp, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(MODEL_VERSION_MAX), nullable=True)
    reasons_json: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint(_in_list("method", ANOMALY_METHODS), name="method_allowed"),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1 AND score = score)",
            name="score_range",
        ),
        CheckConstraint(
            f"model_version IS NULL OR length(model_version) <= {MODEL_VERSION_MAX}",
            name="model_version_length",
        ),
        CheckConstraint("length(reasons_json) > 0", name="reasons_present"),
        Index(
            "anomalies_device_detected",
            "device_id",
            text("detected_at DESC"),
            text("id DESC"),
        ),
    )
