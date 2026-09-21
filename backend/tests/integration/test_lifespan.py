"""The whole process, start to stop, with only the broker faked.

This is the gate BACKEND_SPEC calls the no-broker verification path: the real
lifespan builds the real container, the real Paho adapter drives a fake
transport, and one delivery travels all the way to a persisted row that the
REST API then serves. Startup order, acknowledgement and shutdown are asserted
on the same objects the production entry point uses.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from powerguard.config import Settings
from powerguard.main import create_app
from powerguard.mqtt.topics import SUBSCRIPTIONS, status_topic, telemetry_topic
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


def status_bytes(status: str = "online") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "boot_id": BOOT_ID,
            "firmware_version": FIRMWARE,
        }
    ).encode("utf-8")


@pytest.fixture
def mqtt_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_username="backend",
        mqtt_password="broker-secret-value",
        mqtt_ingress_queue_size=8,
        mqtt_shutdown_grace_s=2.0,
        stale_after_s=60.0,
        stale_scan_interval_s=60.0,
    )


@pytest.fixture
def transport() -> FakeMqttTransport:
    return FakeMqttTransport()


@pytest.fixture
def running(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> Iterator[tuple[TestClient, FakeMqttTransport]]:
    command.upgrade(alembic_config(mqtt_settings.database_url), "head")
    app = create_app(mqtt_settings, mqtt_transport_factory=lambda _s: transport)
    with TestClient(app) as client:
        yield client, transport


def wait_for(client: TestClient, path: str, predicate: Any, tries: int = 200) -> Any:
    """Poll the API until ingestion has committed, or fail loudly."""
    for _ in range(tries):
        body = client.get(path).json()
        if predicate(body):
            return body
        client.get("/api/v1/health")
    raise AssertionError(f"condition never held for {path}: {body}")


def test_startup_connects_the_adapter_and_subscribes(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, transport = running
    assert transport.loop_started == 1
    assert transport.manual_ack is True

    transport.connect_and_subscribe()
    assert transport.subscriptions == list(SUBSCRIPTIONS)
    assert client.app.state.container.mqtt_connected() is True  # type: ignore[attr-defined]


def test_health_reports_mqtt_state_from_the_adapter(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, transport = running
    assert client.get("/api/v1/health").json()["mqtt"] == "disconnected"

    transport.connect_and_subscribe()
    assert client.get("/api/v1/health").json()["mqtt"] == "connected"

    transport.drop_connection()
    body = client.get("/api/v1/health").json()
    assert body["mqtt"] == "disconnected"
    # A broker outage is not an unhealthy service: history stays readable.
    assert body["status"] == "ok"


def test_one_delivery_becomes_a_row_the_api_serves_and_is_acknowledged(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, transport = running
    transport.connect_and_subscribe()

    mid = transport.deliver(telemetry_topic(DEVICE_ID), telemetry_bytes(seq=7))

    body = wait_for(
        client,
        f"/api/v1/devices/{DEVICE_ID}/telemetry",
        lambda page: len(page["items"]) == 1,
    )
    row = body["items"][0]
    assert row["seq"] == 7
    assert row["voltage_v"] == pytest.approx(7.84)
    assert row["received_at"].endswith("Z")
    assert row["anomaly"] is None  # no model in this phase

    assert transport.acks == [(mid, 1)]


def test_duplicate_delivery_is_acknowledged_without_a_second_row(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, transport = running
    transport.connect_and_subscribe()

    payload = telemetry_bytes(seq=3)
    first = transport.deliver(telemetry_topic(DEVICE_ID), payload)
    wait_for(
        client,
        f"/api/v1/devices/{DEVICE_ID}/telemetry",
        lambda page: len(page["items"]) == 1,
    )
    second = transport.deliver(telemetry_topic(DEVICE_ID), payload)

    body = wait_for(
        client,
        f"/api/v1/devices/{DEVICE_ID}/telemetry",
        lambda page: transport.acks == [(first, 1), (second, 1)],
    )
    assert len(body["items"]) == 1, "the idempotency key stopped the second insert"


def test_invalid_payload_is_acknowledged_and_stored_nowhere(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, transport = running
    transport.connect_and_subscribe()

    mid = transport.deliver(telemetry_topic(DEVICE_ID), b"{not json")

    for _ in range(200):
        if transport.acks:
            break
        client.get("/api/v1/health")
    assert transport.acks == [(mid, 1)], "a poison message must not loop forever"
    assert client.get("/api/v1/devices").json()["items"] == []


def test_retained_status_registers_the_device(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, transport = running
    transport.connect_and_subscribe()

    transport.deliver(status_topic(DEVICE_ID), status_bytes("online"), retain=True)

    body = wait_for(client, "/api/v1/devices", lambda page: len(page["items"]) == 1)
    assert body["items"][0]["id"] == DEVICE_ID
    assert body["items"][0]["status"] == "online"
    assert body["items"][0]["latest"] is None


def test_shutdown_drains_acknowledges_then_leaves_the_session(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    command.upgrade(alembic_config(mqtt_settings.database_url), "head")
    app = create_app(mqtt_settings, mqtt_transport_factory=lambda _s: transport)

    with TestClient(app) as client:
        transport.connect_and_subscribe()
        mid = transport.deliver(telemetry_topic(DEVICE_ID), telemetry_bytes(seq=11))
        wait_for(
            client,
            f"/api/v1/devices/{DEVICE_ID}/telemetry",
            lambda page: len(page["items"]) == 1,
        )

    assert transport.unsubscriptions == list(SUBSCRIPTIONS)
    assert (mid, 1) in transport.acks
    assert transport.trace.index(f"ack:{mid}") < transport.trace.index("disconnect")
    assert transport.trace.index("disconnect") < transport.trace.index("loop_stop")


def test_the_broker_password_never_reaches_a_response(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    client, _transport = running
    body = client.get("/api/v1/health").text
    assert "broker-secret-value" not in body


def test_stale_scan_task_is_started_and_cancelled(
    mqtt_settings: Settings, transport: FakeMqttTransport
) -> None:
    command.upgrade(alembic_config(mqtt_settings.database_url), "head")
    app = create_app(mqtt_settings, mqtt_transport_factory=lambda _s: transport)

    with TestClient(app):
        container = app.state.container
        assert len(container.background) == 1
        assert not container.background[0].done()
        tasks = list(container.background)

    assert container.background == []
    assert all(task.cancelled() or task.done() for task in tasks)
    assert container.mqtt is None


def test_seconds_are_server_time_not_payload_time(
    running: tuple[TestClient, FakeMqttTransport],
) -> None:
    """ADR-006: received_at is stamped at the edge, sampled_at stays diagnostic."""
    client, transport = running
    transport.connect_and_subscribe()
    before = dt.datetime.now(dt.UTC)

    transport.deliver(
        telemetry_topic(DEVICE_ID), telemetry_bytes(seq=1, sampled_at="2020-01-01T00:00:00.000Z")
    )
    body = wait_for(
        client,
        f"/api/v1/devices/{DEVICE_ID}/latest",
        lambda row: row.get("seq") == 1,
    )

    assert body["sampled_at"] == "2020-01-01T00:00:00.000Z"
    received = dt.datetime.strptime(body["received_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=dt.UTC
    )
    assert received >= before - dt.timedelta(seconds=1)
