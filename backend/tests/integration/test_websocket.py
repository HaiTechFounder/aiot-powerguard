"""WebSocket v1 endpoint: admission control and live delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from alembic import command
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from powerguard.api.routes.websocket import _pump
from powerguard.config import Settings
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.services import FixedClock
from powerguard.main import create_app
from powerguard.realtime.hub import EventHub
from tests import builders
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.conftest import alembic_config, make_settings

WS_URL = f"/ws/v1/devices/{DEVICE_ID}"


@pytest.fixture
def client(
    settings: Settings, uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> Iterator[TestClient]:
    command.upgrade(alembic_config(settings.database_url), "head")
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, clock.now())
        uow.commit()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# The handshake is accepted before the verdict is sent, so the close arrives on
# the first receive rather than at connect time. That is deliberate: see the
# module docstring in `api/routes/websocket.py` and
# `test_websocket_real_server.py`, which proves the code survives a real server.
def test_unknown_device_is_closed_with_4404(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect(
        "/ws/v1/devices/nobody"
    ) as socket:
        socket.receive_text()
    assert excinfo.value.code == 4404


def test_malformed_device_id_is_closed_with_4400(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect(
        "/ws/v1/devices/not a device"
    ) as socket:
        socket.receive_text()
    assert excinfo.value.code == 4400


def test_known_device_receives_committed_events(client: TestClient, clock: FixedClock) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    with client.websocket_connect(WS_URL) as session:
        # Publishing must happen on the application loop, which is where the
        # hub and every subscription queue live.
        session.portal.call(
            container.hub.publish_telemetry,
            builders.telemetry(seq=11, received_at=clock.now()),
        )
        frame = session.receive_json()

    assert frame["schema_version"] == 1
    assert frame["type"] == "telemetry"
    assert frame["emitted_at"].endswith("Z")
    assert frame["data"]["device_id"] == DEVICE_ID
    assert frame["data"]["seq"] == 11


def test_events_for_other_devices_are_not_delivered(
    client: TestClient, clock: FixedClock
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    with client.websocket_connect(WS_URL) as session:
        session.portal.call(
            container.hub.publish_telemetry,
            builders.telemetry(seq=1, received_at=clock.now(), device_id="powerguard-02"),
        )
        session.portal.call(
            container.hub.publish_telemetry,
            builders.telemetry(seq=2, received_at=clock.now()),
        )
        frame = session.receive_json()

    # The first event belonged to another device and must not appear.
    assert frame["data"]["seq"] == 2


def test_connection_slot_is_released_on_disconnect(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    with client.websocket_connect(WS_URL):
        assert container.hub.connection_count == 1
    # The finally block always unsubscribes.
    assert container.hub.connection_count == 0
    assert container.hub.counters.opened == 1
    assert container.hub.counters.closed == 1


@pytest.fixture
def tiny_queue_client(
    database_url: str, uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> Iterator[TestClient]:
    """A client whose outbound queue overflows after a single buffered event."""
    settings = make_settings(database_url, ws_queue_size=1)
    command.upgrade(alembic_config(settings.database_url), "head")
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, clock.now())
        uow.commit()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


async def _publish_burst(hub: EventHub, clock: FixedClock, count: int) -> None:
    """Publish without yielding, so the consumer cannot drain between events."""
    for seq in range(count):
        await hub.publish_telemetry(builders.telemetry(seq=seq, received_at=clock.now()))


def test_slow_client_is_actually_closed_with_1013(
    tiny_queue_client: TestClient, clock: FixedClock
) -> None:
    """The close frame must arrive, not merely be intended.

    The dropped subscription has a full queue, so nothing can be enqueued to
    wake its pump: only an explicit close signal ends the connection.
    """
    container = tiny_queue_client.app.state.container  # type: ignore[attr-defined]
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        tiny_queue_client.websocket_connect(WS_URL) as session,
    ):
        session.portal.call(_publish_burst, container.hub, clock, 4)
        while True:
            session.receive_json()

    assert excinfo.value.code == 1013
    assert container.hub.counters.slow_clients_dropped == 1
    assert container.hub.connection_count == 0


class StubWebSocket:
    """The three WebSocket calls the pump makes, and nothing else."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def send_json(self, data: dict[str, object]) -> None:
        self.sent.append(data)

    async def receive(self) -> dict[str, object]:
        return await self.incoming.get()


async def test_the_pump_notices_a_disconnect_with_no_traffic(clock: FixedClock) -> None:
    """v1 is send-only, so a disconnect is only noticed if it is watched for.

    Nothing is ever published here: without the receive watchdog the pump would
    stay parked on an empty queue and the connection slot would leak until the
    next event for that device happened to arrive.
    """
    hub = EventHub(clock=clock, max_connections=4, queue_size=4)
    subscription = hub.subscribe(DEVICE_ID)
    socket = StubWebSocket()

    pump = asyncio.ensure_future(_pump(socket, subscription))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert not pump.done()

    await socket.incoming.put({"type": "websocket.disconnect", "code": 1001})

    with pytest.raises(WebSocketDisconnect) as excinfo:
        await asyncio.wait_for(pump, timeout=2)
    assert excinfo.value.code == 1001
    assert socket.sent == []


async def test_the_pump_forwards_events_until_the_hub_closes_it(
    clock: FixedClock,
) -> None:
    hub = EventHub(clock=clock, max_connections=4, queue_size=4)
    subscription = hub.subscribe(DEVICE_ID)
    socket = StubWebSocket()

    pump = asyncio.ensure_future(_pump(socket, subscription))  # type: ignore[arg-type]
    await hub.publish_telemetry(builders.telemetry(seq=5, received_at=clock.now()))
    await asyncio.sleep(0)

    hub.unsubscribe(subscription)
    await asyncio.wait_for(pump, timeout=2)

    assert [frame["data"]["seq"] for frame in socket.sent] == [5]  # type: ignore[index]
