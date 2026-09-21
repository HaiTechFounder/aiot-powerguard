"""FastAPI application factory.

Importing this module must not read secrets, open a database or touch the
network. Everything with a lifetime is created in the lifespan handler, which
Block 2 extends with MQTT ingestion and the WebSocket hub.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from powerguard import __version__
from powerguard.api.errors import install_error_handlers
from powerguard.api.router import api_router
from powerguard.api.routes import websocket
from powerguard.config import Settings, get_settings
from powerguard.mqtt.client import TransportFactory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from powerguard.bootstrap import build_container, shutdown_container

    settings: Settings = app.state.settings
    container = await build_container(
        settings, transport_factory=getattr(app.state, "mqtt_transport_factory", None)
    )
    app.state.container = container
    try:
        yield
    finally:
        await shutdown_container(container)


def create_app(
    settings: Settings | None = None,
    *,
    mqtt_transport_factory: TransportFactory | None = None,
) -> FastAPI:
    """Build the application.

    Tests inject their own settings and, for the adapter lifecycle gate, a fake
    MQTT transport that the real adapter drives through the real lifespan.
    """
    resolved = settings or get_settings()

    app = FastAPI(
        title="AIoT PowerGuard",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.mqtt_transport_factory = mqtt_transport_factory

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    install_error_handlers(app)
    app.include_router(api_router)
    app.include_router(websocket.router)

    return app
