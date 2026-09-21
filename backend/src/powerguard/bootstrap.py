"""Composition root.

The only place that wires adapters to the domain, and the only place that owns
resource lifetimes. Startup order follows BACKEND_SPEC section 6: settings and
logging, engine and migration check, repositories and services, then the MQTT
adapter and background tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from powerguard.config import Settings
from powerguard.db.engine import create_database_engine, create_session_factory
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.device_status import DeviceStatusTracker
from powerguard.domain.entities import Device, DeviceStatus
from powerguard.domain.ports import Clock, InferenceEngine
from powerguard.domain.services import SystemClock
from powerguard.inference.unavailable import UnavailableInference
from powerguard.mqtt.client import MqttCounters, PahoMqttAdapter, TransportFactory
from powerguard.mqtt.ingestion import IngestionService
from powerguard.mqtt.ports import AckDecision, InboundMessage
from powerguard.observability import log_event
from powerguard.realtime.hub import EventHub

logger = logging.getLogger(__name__)

EXPECTED_ALEMBIC_HEAD = "0001_initial_schema"


class MigrationError(RuntimeError):
    """The database schema is not at the revision this build expects."""


@dataclass(slots=True)
class StaleCounters:
    """Stale-scan diagnostics required by BACKEND_SPEC 13.

    ``transitions`` counts the ones this scan caused. Every transition, whatever
    caused it, is also counted once by the shared `DeviceStatusTracker`.
    """

    scans: int = 0
    transitions: int = 0
    failures: int = 0


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: Engine
    uow_factory: SqlUnitOfWorkFactory
    clock: Clock
    inference: InferenceEngine
    hub: EventHub
    ingestion: IngestionService
    status: DeviceStatusTracker
    stale: StaleCounters = field(default_factory=StaleCounters)
    background: list[asyncio.Task[None]] = field(default_factory=list)
    mqtt: PahoMqttAdapter | None = None
    _mqtt_connected: bool = False

    def mqtt_connected(self) -> bool:
        """Adapter state, reported independently of HTTP/database health."""
        if not self.settings.mqtt_enabled:
            return False
        return self._mqtt_connected

    def set_mqtt_connected(self, connected: bool) -> None:
        self._mqtt_connected = connected

    @property
    def mqtt_counters(self) -> MqttCounters | None:
        """Adapter counters, or None when MQTT is disabled for this process."""
        return self.mqtt.counters if self.mqtt is not None else None


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
        ).scalar_one_or_none()
        if exists is None:
            return None
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def verify_migrations(engine: Engine, expected: str = EXPECTED_ALEMBIC_HEAD) -> None:
    """Refuse to start on an unapplied migration. Startup never auto-migrates."""
    revision = current_revision(engine)
    if revision is None:
        raise MigrationError("database has no schema; run `python -m alembic upgrade head`")
    if revision != expected:
        raise MigrationError(
            f"database is at revision {revision!r}, this build expects {expected!r}; "
            "run `python -m alembic upgrade head`"
        )


async def _stale_scan_loop(container: Container) -> None:
    """Move silent online devices to stale, and broadcast real transitions."""
    settings = container.settings
    while True:
        try:
            await asyncio.sleep(settings.stale_scan_interval_s)
            container.stale.scans += 1
            cutoff = container.clock.now() - dt.timedelta(seconds=settings.stale_after_s)
            transitioned = await asyncio.to_thread(_mark_stale, container, cutoff)
            for device in transitioned:
                # Same transition path as telemetry and status messages, so the
                # event and the counter are emitted in exactly one place.
                changed = container.status.record(
                    device.id, DeviceStatus.STALE, source="stale_scan"
                )
                if not changed:
                    continue
                container.stale.transitions += 1
                await container.hub.publish_device_status(device)
        except asyncio.CancelledError:
            raise
        except Exception:
            container.stale.failures += 1
            log_event(logger, logging.ERROR, "stale_scan_failed")
            # Logged and retried on the next interval; HTTP and ingestion run on.
            logger.exception("stale_scan_failed_detail")


def _mark_stale(container: Container, cutoff: dt.datetime) -> list[Device]:
    with container.uow_factory() as uow:
        ids = list(uow.devices.mark_stale(cutoff))
        uow.commit()
        # Re-read inside the same unit of work so only committed state is sent.
        devices = [uow.devices.get(device_id) for device_id in ids]
    return [device for device in devices if device is not None]


async def build_container(
    settings: Settings, *, transport_factory: TransportFactory | None = None
) -> Container:
    """Wire the process.

    ``transport_factory`` replaces the Paho client the MQTT adapter drives. The
    verification suite injects a fake transport here so the whole adapter —
    connect handling, subscriptions, queueing, acknowledgement, saturation,
    redelivery and shutdown — runs under test without a broker.
    """
    engine = create_database_engine(settings)
    verify_migrations(engine)
    session_factory = create_session_factory(engine)
    uow_factory = SqlUnitOfWorkFactory(session_factory)
    clock = SystemClock()
    inference = UnavailableInference()
    hub = EventHub(
        clock=clock,
        max_connections=settings.ws_max_connections,
        queue_size=settings.ws_queue_size,
    )
    status = DeviceStatusTracker()
    ingestion = IngestionService(
        settings=settings,
        uow_factory=uow_factory,
        clock=clock,
        inference=inference,
        publisher=hub,
        status=status,
    )

    container = Container(
        settings=settings,
        engine=engine,
        uow_factory=uow_factory,
        clock=clock,
        inference=inference,
        hub=hub,
        ingestion=ingestion,
        status=status,
    )
    container.background.append(asyncio.create_task(_stale_scan_loop(container)))

    if settings.mqtt_enabled:
        async def handle(message: InboundMessage) -> AckDecision:
            return (await ingestion.handle(message)).ack

        adapter = PahoMqttAdapter(
            settings,
            handle,
            on_connection_change=container.set_mqtt_connected,
            transport_factory=transport_factory,
        )
        container.mqtt = adapter
        await adapter.start()

    log_event(logger, logging.INFO, "startup_complete", mqtt_enabled=settings.mqtt_enabled)
    return container


async def shutdown_container(container: Container) -> None:
    # Stop new intake first, then drain what was already accepted.
    if container.mqtt is not None:
        await container.mqtt.stop()
        container.mqtt = None
        container.set_mqtt_connected(False)
    for task in container.background:
        task.cancel()
    for task in container.background:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    container.background.clear()
    container.engine.dispose()
    log_event(logger, logging.INFO, "shutdown_complete")
