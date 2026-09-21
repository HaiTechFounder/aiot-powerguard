"""Restart recovery: the database is the truth across process lifetimes.

Everything the adapter, the hub and the trackers hold is process-local. A
restart throws all of it away, so the only thing that may survive is what was
committed. These tests run the real lifespan twice over one database file and
assert exactly that — including that idempotency still holds afterwards, which
is the property a QoS 1 redelivery after a crash depends on.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from powerguard.config import Settings
from powerguard.domain.entities import DeviceStatus
from powerguard.main import create_app
from powerguard.mqtt.topics import status_topic, telemetry_topic
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.conftest import alembic_config, make_settings
from tests.fakes.fake_transport import FakeMqttTransport


def telemetry_bytes(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "boot_id": BOOT_ID,
        "seq": 1,
        "sampled_at": None,
        "voltage_v": 7.84,
        "current_a": 0.417,
        "power_w": 3.269,
        "energy_wh": 0.284,
        "sensor_status": "ok",
        "firmware_version": FIRMWARE,
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def status_bytes(status: str = "online", boot: str = BOOT_ID) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "boot_id": boot,
            "firmware_version": FIRMWARE,
        }
    ).encode("utf-8")


@pytest.fixture
def restart_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="broker-secret-value",
        stale_after_s=60.0,
        stale_scan_interval_s=60.0,
    )


@pytest.fixture
def migrated(restart_settings: Settings) -> Iterator[Settings]:
    command.upgrade(alembic_config(restart_settings.database_url), "head")
    yield restart_settings


@contextmanager
def run_process(settings: Settings) -> Iterator[tuple[TestClient, FakeMqttTransport]]:
    """One backend lifetime: a fresh app, container and transport.

    A context manager, so the lifespan is always shut down before the next
    process opens the same database file.
    """
    transport = FakeMqttTransport()
    app = create_app(settings, mqtt_transport_factory=lambda _s: transport)
    with TestClient(app) as client:
        transport.connect_and_subscribe()
        yield client, transport


def wait_for(client: TestClient, path: str, predicate: Any, tries: int = 200) -> Any:
    for _ in range(tries):
        body = client.get(path).json()
        if predicate(body):
            return body
        client.get("/api/v1/health")
    raise AssertionError(f"condition never held for {path}: {body}")


def ingest(
    client: TestClient, transport: FakeMqttTransport, payload: bytes, expected_rows: int
) -> None:
    transport.deliver(telemetry_topic(DEVICE_ID), payload)
    wait_for(
        client,
        f"/api/v1/devices/{DEVICE_ID}/telemetry",
        lambda page: len(page["items"]) == expected_rows,
    )


def test_committed_rows_survive_a_restart(migrated: Settings) -> None:
    with run_process(migrated) as (client, transport):
        ingest(client, transport, telemetry_bytes(seq=1), 1)
        ingest(client, transport, telemetry_bytes(seq=2), 2)

    # A second process over the same file.
    with run_process(migrated) as (client, _transport):
        body = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry").json()
        assert [row["seq"] for row in body["items"]] == [2, 1]
        assert client.get("/api/v1/health").json()["database"] == "ready"


def test_idempotency_still_holds_after_a_restart(migrated: Settings) -> None:
    """A QoS 1 redelivery across a crash must not become a second row."""
    payload = telemetry_bytes(seq=9)
    with run_process(migrated) as (client, transport):
        ingest(client, transport, payload, 1)

    with run_process(migrated) as (client, transport):
        mid = transport.deliver(telemetry_topic(DEVICE_ID), payload)
        for _ in range(200):
            if transport.acks:
                break
            client.get("/api/v1/health")

        assert transport.acks == [(mid, 1)], "the redelivery is acknowledged"
        body = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry").json()
        assert len(body["items"]) == 1, "and produces no second row"
        assert client.app.state.container.ingestion.counters.duplicate_telemetry == 1


def test_a_new_process_starts_from_an_empty_sequence_memory(
    migrated: Settings,
) -> None:
    """Gap counting is process-local diagnostics, not persisted state."""
    with run_process(migrated) as (client, transport):
        ingest(client, transport, telemetry_bytes(seq=1), 1)
        ingest(client, transport, telemetry_bytes(seq=5), 2)
        assert client.app.state.container.ingestion.counters.sequence_gaps == 1

    with run_process(migrated) as (client, transport):
        counters = client.app.state.container.ingestion.counters
        assert counters.sequence_gaps == 0
        assert counters.accepted_telemetry == 0

        # The rows are all still there, and a later sequence appends cleanly.
        ingest(client, transport, telemetry_bytes(seq=6), 3)
        body = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry").json()
        assert [row["seq"] for row in body["items"]] == [6, 5, 1]
        assert counters.sequence_gaps == 0, "seq 6 follows seq 5 in the database"


def test_device_status_is_read_from_the_row_not_from_memory(
    migrated: Settings,
) -> None:
    with run_process(migrated) as (client, transport):
        transport.deliver(status_topic(DEVICE_ID), status_bytes("offline"), retain=True)
        wait_for(
            client,
            "/api/v1/devices",
            lambda page: page["items"] and page["items"][0]["status"] == "offline",
        )

    with run_process(migrated) as (client, _transport):
        body = client.get("/api/v1/devices").json()
        assert body["items"][0]["status"] == DeviceStatus.OFFLINE.value
        # The tracker is empty, so the next transition is reported as new.
        assert client.app.state.container.status.current(DEVICE_ID) is None


def test_a_restart_reconnects_and_resubscribes(migrated: Settings) -> None:
    with run_process(migrated) as (client, transport):
        # `start()` records the destination; the scheduler owns the attempt.
        assert transport.connect_calls
        assert client.app.state.container.mqtt_connected() is True

    second = FakeMqttTransport()
    app = create_app(migrated, mqtt_transport_factory=lambda _s: second)
    with TestClient(app) as client:
        assert second.connect_calls, "the new process connects on its own"
        second.connect_and_subscribe()
        assert client.app.state.container.mqtt_connected() is True
        assert len(second.subscriptions) == 2


def test_history_written_before_the_restart_is_still_queryable_by_window(
    migrated: Settings,
) -> None:
    before = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    with run_process(migrated) as (client, transport):
        for seq in range(1, 4):
            ingest(client, transport, telemetry_bytes(seq=seq), seq)

    window = f"?from={before.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z"
    with run_process(migrated) as (client, _transport):
        body = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry{window}").json()
        assert len(body["items"]) == 3


def test_startup_refuses_a_database_that_was_never_migrated(
    restart_settings: Settings,
) -> None:
    """No migration, no service: startup never creates schema by itself."""
    transport = FakeMqttTransport()
    app = create_app(restart_settings, mqtt_transport_factory=lambda _s: transport)

    with pytest.raises(Exception, match="alembic upgrade head"), TestClient(app):
        pass  # pragma: no cover - the context never opens
