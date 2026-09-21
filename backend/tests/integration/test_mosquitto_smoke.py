"""Opt-in smoke test against a real Mosquitto broker.

Everything else in this suite injects a fake transport. This one uses real Paho
clients end to end — device -> broker -> backend -> SQLite -> REST — and is the
only test that can prove the parts a fake cannot: a real CONNACK, a real SUBACK,
real QoS 1 delivery, a real retained LWT, and a WebSocket frame that could only
have come from a message that really crossed the broker.

It is skipped unless a broker is deliberately made available, and the skip
message carries the exact command to enable it. A skipped run is recorded as
NOT_RUN, never as a pass.

Enable it with:

    $env:POWERGUARD_SMOKE_BROKER = "1"
    $env:POWERGUARD_SMOKE_USERNAME = "powerguard-backend"
    $env:POWERGUARD_SMOKE_PASSWORD = "<the password you created>"
    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_mosquitto_smoke.py -m integration

See deploy/mosquitto/README.md for starting the broker, natively or with Docker.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from powerguard.config import Settings
from powerguard.main import create_app
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.conftest import alembic_config, make_settings

pytestmark = pytest.mark.integration

ENABLE = "POWERGUARD_SMOKE_BROKER"
HOST = os.environ.get("POWERGUARD_SMOKE_HOST", "127.0.0.1")
PORT = int(os.environ.get("POWERGUARD_SMOKE_PORT", "1883"))
USERNAME = os.environ.get("POWERGUARD_SMOKE_USERNAME", "powerguard-backend")
DEVICE_USERNAME = os.environ.get("POWERGUARD_SMOKE_DEVICE_USERNAME", "powerguard-device")

ENABLE_COMMAND = (
    '$env:POWERGUARD_SMOKE_BROKER = "1"; '
    '$env:POWERGUARD_SMOKE_USERNAME = "powerguard-backend"; '
    '$env:POWERGUARD_SMOKE_PASSWORD = "<password>"; '
    ".\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_mosquitto_smoke.py "
    "-m integration"
)


def broker_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def skip_reason() -> str | None:
    """Why this test cannot run, or None when it can."""
    if os.environ.get(ENABLE) != "1":
        return f"broker smoke not enabled (NOT_RUN). Enable with: {ENABLE_COMMAND}"
    if not os.environ.get("POWERGUARD_SMOKE_PASSWORD"):
        return "POWERGUARD_SMOKE_PASSWORD is not set (NOT_RUN)"
    if not broker_reachable(HOST, PORT):
        return f"no broker on {HOST}:{PORT} (NOT_RUN). See deploy/mosquitto/README.md"
    return None


pytest.importorskip("paho.mqtt.client", reason="paho-mqtt is required for the smoke test")


@pytest.fixture(autouse=True)
def _require_broker() -> None:
    reason = skip_reason()
    if reason is not None:
        pytest.skip(reason)


def telemetry_bytes(seq: int = 1, **overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "schema_version": 1,
        "boot_id": BOOT_ID,
        "seq": seq,
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


def status_bytes(status: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "boot_id": BOOT_ID,
            "firmware_version": FIRMWARE,
        }
    ).encode("utf-8")


@pytest.fixture
def smoke_settings(database_url: str) -> Settings:
    return make_settings(
        database_url,
        mqtt_enabled=True,
        mqtt_host=HOST,
        mqtt_port=PORT,
        mqtt_username=USERNAME,
        mqtt_password=os.environ["POWERGUARD_SMOKE_PASSWORD"],
        mqtt_instance_id="smoke",
        stale_after_s=60.0,
        stale_scan_interval_s=60.0,
    )


@pytest.fixture
def backend(smoke_settings: Settings) -> Iterator[TestClient]:
    command.upgrade(alembic_config(smoke_settings.database_url), "head")
    with TestClient(create_app(smoke_settings)) as client:
        # The real adapter needs a real CONNACK and SUBACK before it is usable.
        for _ in range(100):
            if client.get("/api/v1/health").json()["mqtt"] == "connected":
                break
            time.sleep(0.1)
        else:  # pragma: no cover - only when the broker refuses the session
            pytest.fail("the backend never confirmed its subscriptions")
        yield client


@pytest.fixture
def device() -> Iterator[Any]:
    """A real device client, with a retained offline LWT."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"powerguard-device-{DEVICE_ID}",
        protocol=mqtt.MQTTv311,
    )
    password = os.environ.get("POWERGUARD_SMOKE_DEVICE_PASSWORD") or os.environ[
        "POWERGUARD_SMOKE_PASSWORD"
    ]
    client.username_pw_set(DEVICE_USERNAME, password)
    client.will_set(
        f"powerguard/v1/devices/{DEVICE_ID}/status",
        status_bytes("offline"),
        qos=1,
        retain=True,
    )
    client.connect(HOST, PORT, keepalive=30)
    client.loop_start()
    try:
        yield client
    finally:
        client.loop_stop()
        client.disconnect()


def publish(device: Any, suffix: str, body: bytes, retain: bool = False) -> None:
    info = device.publish(
        f"powerguard/v1/devices/{DEVICE_ID}/{suffix}", body, qos=1, retain=retain
    )
    info.wait_for_publish(timeout=5)


def wait_for(client: TestClient, path: str, predicate: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    body: Any = None
    while time.monotonic() < deadline:
        body = client.get(path).json()
        if predicate(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"condition never held for {path}: {body}")


def test_valid_duplicate_and_malformed_over_a_real_broker(
    backend: TestClient, device: Any
) -> None:
    """One pass over the outcome matrix, with exact row counts."""
    publish(device, "status", status_bytes("online"), retain=True)
    publish(device, "telemetry", telemetry_bytes(seq=1))
    publish(device, "telemetry", telemetry_bytes(seq=2))

    history = f"/api/v1/devices/{DEVICE_ID}/telemetry"
    wait_for(backend, history, lambda page: len(page["items"]) == 2)

    # A redelivery of an identical reading is one row, not two.
    publish(device, "telemetry", telemetry_bytes(seq=2))
    time.sleep(0.5)
    body = backend.get(history).json()
    assert len(body["items"]) == 2
    assert [row["seq"] for row in body["items"]] == [2, 1]

    # A malformed payload is acknowledged and stored nowhere.
    publish(device, "telemetry", b"{not json")
    time.sleep(0.5)
    assert len(backend.get(history).json()["items"]) == 2

    counters = backend.app.state.container.ingestion.counters  # type: ignore[attr-defined]
    assert counters.accepted_telemetry == 2
    assert counters.duplicate_telemetry == 1
    assert counters.rejected == 1

    device_row = backend.get("/api/v1/devices").json()["items"][0]
    assert device_row["id"] == DEVICE_ID
    assert device_row["status"] == "online"


def test_a_retained_lwt_marks_the_device_offline(
    backend: TestClient, device: Any
) -> None:
    """The broker publishes the will when the device disappears uncleanly."""
    publish(device, "status", status_bytes("online"), retain=True)
    wait_for(
        backend,
        "/api/v1/devices",
        lambda page: page["items"] and page["items"][0]["status"] == "online",
    )

    # No DISCONNECT packet: the broker must publish the will.
    device._sock_close()

    wait_for(
        backend,
        "/api/v1/devices",
        lambda page: page["items"] and page["items"][0]["status"] == "offline",
        timeout=20.0,
    )


def test_the_backend_recovers_its_subscriptions_after_a_reconnect(
    backend: TestClient, device: Any
) -> None:
    container = backend.app.state.container  # type: ignore[attr-defined]
    adapter = container.mqtt
    assert adapter is not None

    publish(device, "telemetry", telemetry_bytes(seq=1))
    history = f"/api/v1/devices/{DEVICE_ID}/telemetry"
    wait_for(backend, history, lambda page: len(page["items"]) == 1)

    connected_before = adapter.counters.connected
    adapter.request_reconnect("smoke test")

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if adapter.counters.connected > connected_before:
            break
        time.sleep(0.1)
    else:  # pragma: no cover - only when the broker never answers again
        pytest.fail("the adapter never re-established a confirmed session")

    # A reading published after the new session still lands.
    publish(device, "telemetry", telemetry_bytes(seq=2))
    wait_for(backend, history, lambda page: len(page["items"]) == 2)
    assert adapter.counters.subscribed >= 4, "both filters confirmed twice"


def test_a_reading_over_the_real_broker_reaches_a_websocket_client(
    backend: TestClient, device: Any
) -> None:
    """The Phase 03 contract's live path, end to end and unfaked.

    device -> broker -> backend -> hub -> WebSocket. Nothing is injected into
    the hub: the only way this frame appears is if a real QoS 1 publication was
    ingested, committed and broadcast.
    """
    # The device must exist before a socket may subscribe to it (4404 otherwise).
    publish(device, "status", status_bytes("online"), retain=True)
    wait_for(backend, "/api/v1/devices", lambda page: len(page["items"]) == 1)

    with backend.websocket_connect(f"/ws/v1/devices/{DEVICE_ID}") as session:
        publish(device, "telemetry", telemetry_bytes(seq=41))

        frame = session.receive_json()

    assert frame["schema_version"] == 1
    assert frame["type"] == "telemetry"
    assert frame["emitted_at"].endswith("Z")
    assert frame["data"]["device_id"] == DEVICE_ID
    assert frame["data"]["seq"] == 41
    assert frame["data"]["voltage_v"] == pytest.approx(7.84)

    # The same reading is durable, not just broadcast.
    body = wait_for(
        backend,
        f"/api/v1/devices/{DEVICE_ID}/telemetry",
        lambda page: len(page["items"]) == 1,
    )
    assert body["items"][0]["seq"] == 41


def test_a_duplicate_over_the_real_broker_produces_no_second_frame(
    backend: TestClient, device: Any
) -> None:
    """API_CONTRACT: a duplicate is acknowledged but never broadcast."""
    publish(device, "status", status_bytes("online"), retain=True)
    wait_for(backend, "/api/v1/devices", lambda page: len(page["items"]) == 1)

    payload = telemetry_bytes(seq=77)
    with backend.websocket_connect(f"/ws/v1/devices/{DEVICE_ID}") as session:
        publish(device, "telemetry", payload)
        first = session.receive_json()
        assert first["data"]["seq"] == 77

        publish(device, "telemetry", payload)  # same (device, boot, seq)
        publish(device, "telemetry", telemetry_bytes(seq=78))

        # The next frame is the new reading, not a repeat of the duplicate.
        second = session.receive_json()

    assert second["data"]["seq"] == 78
    counters = backend.app.state.container.ingestion.counters  # type: ignore[attr-defined]
    assert counters.duplicate_telemetry == 1


def test_an_unknown_device_is_refused_by_the_websocket(backend: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo, backend.websocket_connect(
        "/ws/v1/devices/never-seen"
    ):
        pass  # pragma: no cover - the context never opens

    assert excinfo.value.code == 4404
