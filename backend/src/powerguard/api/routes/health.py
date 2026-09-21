"""Health endpoint.

HTTP and database health decide the status code. An MQTT or model outage is
reported independently and never turns the service unhealthy: history stays
readable from the database while the broker is down.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from powerguard import __version__
from powerguard.api.dependencies import ContainerDep, run_in_db_thread
from powerguard.api.schemas import HealthDto

logger = logging.getLogger(__name__)
router = APIRouter()


def _probe_database(container: ContainerDep) -> bool:
    try:
        with container.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health_database_probe_failed")
        return False
    return True


@router.get("/health", response_model=HealthDto, tags=["health"])
async def health(container: ContainerDep, response: Response) -> HealthDto:
    database_ready = await run_in_db_thread(_probe_database, container)
    if not database_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthDto(
        status="ok" if database_ready else "degraded",
        database="ready" if database_ready else "unavailable",
        mqtt="connected" if container.mqtt_connected() else "disconnected",
        model=container.inference.readiness(),
        version=__version__,
    )
