"""WebSocket close codes as a *real* ASGI server delivers them.

`TestClient` reads the ASGI `websocket.close` message directly, so it reports
an application close code even when the handshake never completed. A real
server cannot: with no handshake there is no close frame, so it answers
HTTP 403 and the browser reports 1006.

That difference is invisible to every other test in this suite and is exactly
what the dashboard depends on — 4404 and 4400 stop it retrying, 1006 does not.
So these run against uvicorn over a real socket.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator

import pytest
import uvicorn
import websockets
from alembic import command
from websockets.exceptions import InvalidStatus

from powerguard.api.routes.websocket import (
    CLOSE_INVALID_DEVICE_ID,
    CLOSE_UNKNOWN_DEVICE,
)
from powerguard.config import Settings
from powerguard.main import create_app
from tests.conftest import alembic_config


@pytest.fixture
def app(settings: Settings):
    command.upgrade(alembic_config(settings.database_url), "head")
    return create_app(settings)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def live_server(app) -> Iterator[str]:
    """Serve `app` on a loopback port for the duration of one test."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.05)
        else:  # pragma: no cover - only on a badly overloaded machine
            pytest.fail("uvicorn did not start")
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _close_code(url: str) -> int:
    """The close code a browser-equivalent client actually observes."""

    async def connect() -> int:
        try:
            async with websockets.connect(url) as socket_:
                await socket_.recv()
        except websockets.ConnectionClosed as closed:
            return int(closed.code)
        except InvalidStatus as rejected:
            # The handshake was refused outright. A browser surfaces this as
            # 1006, which the client treats as a retryable network drop.
            pytest.fail(
                f"handshake rejected with HTTP {rejected.response.status_code}; "
                "a browser would see close 1006 and retry forever"
            )
        raise AssertionError("the server neither closed nor rejected")

    return asyncio.run(connect())


def test_unknown_device_really_delivers_4404(live_server: str) -> None:
    assert _close_code(f"{live_server}/ws/v1/devices/ghost") == CLOSE_UNKNOWN_DEVICE


def test_malformed_device_id_really_delivers_4400(live_server: str) -> None:
    assert _close_code(f"{live_server}/ws/v1/devices/not%20a%20device") == CLOSE_INVALID_DEVICE_ID


def test_a_rejected_socket_is_refused_repeatedly_without_leaking(live_server: str) -> None:
    # A rejection must be cheap and repeatable: the handshake completes, the
    # close frame carries the verdict, and no hub slot is consumed.
    for _ in range(5):
        assert _close_code(f"{live_server}/ws/v1/devices/ghost") == CLOSE_UNKNOWN_DEVICE
