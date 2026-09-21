"""WebSocket v1 endpoint.

The device is validated and looked up before the socket is registered, so an
unknown id closes with 4404 instead of occupying a hub slot. Liveness uses ASGI
ping/pong control frames; v1 defines no JSON ping event.

A rejection still completes the handshake first. Closing before ``accept()``
looks equivalent, and under Starlette's ``TestClient`` it even reports the same
code -- but a real ASGI server has no handshake to put a close frame in, so it
answers HTTP 403 instead and the browser reports 1006. The client cannot tell
1006 from an ordinary network drop, so it would retry a device that can never
exist, forever. Accepting first is what makes 4404 and 4400 reach the client as
the permanent verdicts they are.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from powerguard.api.dependencies import get_container
from powerguard.bootstrap import Container
from powerguard.mqtt.topics import is_valid_device_id
from powerguard.observability import log_event
from powerguard.realtime.hub import CLOSE_SLOW_CLIENT, HubFullError, Subscription

logger = logging.getLogger(__name__)
router = APIRouter()

CLOSE_UNKNOWN_DEVICE = 4404
CLOSE_INVALID_DEVICE_ID = 4400
CLOSE_CAPACITY = 1013


async def _reject(websocket: WebSocket, code: int) -> None:
    """Refuse a connection with a code the client will actually receive.

    See the module docstring: the handshake must complete before a close frame
    can carry an application close code.
    """
    await websocket.accept()
    await websocket.close(code=code)


def _device_exists(container: Container, device_id: str) -> bool:
    with container.uow_factory() as uow:
        return uow.devices.get(device_id) is not None


async def _send_loop(websocket: WebSocket, subscription: Subscription) -> None:
    """Forward events until the hub closes this subscription."""
    while True:
        event = await subscription.next_event()
        if event is None:
            # Dropped by the hub, or the connection is shutting down.
            return
        await websocket.send_json(event.envelope())


async def _watch_client(websocket: WebSocket) -> None:
    """Notice a disconnect on a connection that never sends anything.

    v1 defines no client-to-server messages, so without this the endpoint would
    be send-only and an idle client that went away would keep its slot until
    the next event happened to be published.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(message.get("code", 1005)))


async def _pump(websocket: WebSocket, subscription: Subscription) -> None:
    sender = asyncio.ensure_future(_send_loop(websocket, subscription))
    watcher = asyncio.ensure_future(_watch_client(websocket))
    try:
        done, pending = await asyncio.wait(
            {sender, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            # Re-raise whatever ended the connection, including a disconnect.
            task.result()
    finally:
        for task in (sender, watcher):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@router.websocket("/ws/v1/devices/{device_id}")
async def device_stream(websocket: WebSocket, device_id: str) -> None:
    container = get_container(websocket)  # type: ignore[arg-type]

    if not is_valid_device_id(device_id):
        await _reject(websocket, CLOSE_INVALID_DEVICE_ID)
        container.hub.counters.rejected += 1
        log_event(
            logger, logging.WARNING, "websocket_rejected", reason="malformed device id"
        )
        return

    exists = await asyncio.to_thread(_device_exists, container, device_id)
    if not exists:
        await _reject(websocket, CLOSE_UNKNOWN_DEVICE)
        container.hub.counters.rejected += 1
        log_event(
            logger,
            logging.WARNING,
            "websocket_rejected",
            device_id=device_id,
            reason="unknown device",
        )
        return

    try:
        subscription = container.hub.subscribe(device_id)
    except HubFullError:
        await _reject(websocket, CLOSE_CAPACITY)
        return

    await websocket.accept()
    try:
        await _pump(websocket, subscription)
    except WebSocketDisconnect:
        pass  # the hub records the close when the slot is released
    except asyncio.CancelledError:
        raise
    except Exception:
        log_event(logger, logging.ERROR, "websocket_failed", device_id=device_id)
        logger.exception("websocket_failed_detail device=%s", device_id)
    finally:
        # Always releases the slot, whatever ended the connection.
        container.hub.unsubscribe(subscription)
        if subscription.dropped:
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_SLOW_CLIENT)
        else:
            with contextlib.suppress(Exception):
                await websocket.close()
