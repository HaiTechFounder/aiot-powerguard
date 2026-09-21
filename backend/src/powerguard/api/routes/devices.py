"""Read-only device endpoints.

All database work runs in a worker thread. Pages are newest first and bounded;
``next_before_id`` is only set when the repository proved another page exists.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from powerguard.api.dependencies import (
    ContainerDep,
    DeviceIdDep,
    HistoryQueryDep,
    run_in_db_thread,
)
from powerguard.api.errors import NotFoundError
from powerguard.api.schemas import (
    AnomalyDto,
    AnomalyPageDto,
    DeviceDto,
    DeviceListDto,
    TelemetryDto,
    TelemetryPageDto,
)
from powerguard.bootstrap import Container
from powerguard.domain.entities import Anomaly, Device, Page, Telemetry

router = APIRouter(prefix="/devices", tags=["devices"])


def _persisted_ids(rows: Sequence[Telemetry]) -> list[int]:
    return [row.id for row in rows if row.id is not None]


def _list_devices(
    container: Container,
) -> list[tuple[Device, Telemetry | None, Anomaly | None]]:
    with container.uow_factory() as uow:
        devices = list(uow.devices.list_all())
        latest = {device.id: uow.telemetry.latest_for_device(device.id) for device in devices}
        # The stored verdict travels with the reading it belongs to, resolved
        # in one query for the whole page rather than left as a null.
        anomalies = uow.anomalies.by_telemetry_ids(
            _persisted_ids([row for row in latest.values() if row is not None])
        )
        return [
            (
                device,
                latest[device.id],
                _anomaly_for(anomalies, latest[device.id]),
            )
            for device in devices
        ]


def _anomaly_for(
    anomalies: dict[int, Anomaly], telemetry: Telemetry | None
) -> Anomaly | None:
    if telemetry is None or telemetry.id is None:
        return None
    return anomalies.get(telemetry.id)


def _latest(
    container: Container, device_id: str
) -> tuple[Device | None, Telemetry | None, Anomaly | None]:
    with container.uow_factory() as uow:
        device = uow.devices.get(device_id)
        telemetry = uow.telemetry.latest_for_device(device_id)
        anomaly = (
            uow.anomalies.by_telemetry_id(telemetry.id)
            if telemetry is not None and telemetry.id is not None
            else None
        )
        return device, telemetry, anomaly


def _telemetry_page(
    container: Container, device_id: str, query: HistoryQueryDep
) -> tuple[Device | None, Page[Telemetry], dict[int, Anomaly]]:
    with container.uow_factory() as uow:
        device = uow.devices.get(device_id)
        page = uow.telemetry.history(
            device_id,
            limit=query.limit,
            since=query.since,
            until=query.until,
            before_id=query.before_id,
        )
        anomalies = uow.anomalies.by_telemetry_ids(_persisted_ids(page.items))
        return device, page, anomalies


def _anomaly_page(
    container: Container, device_id: str, query: HistoryQueryDep
) -> tuple[Device | None, Page[Anomaly], dict[int, Telemetry]]:
    """Anomalies with their measurements, read in one transaction."""
    with container.uow_factory() as uow:
        device = uow.devices.get(device_id)
        page = uow.anomalies.history(
            device_id,
            limit=query.limit,
            since=query.since,
            until=query.until,
            before_id=query.before_id,
        )
        # The contract includes the measurements the verdict was based on.
        measurements: dict[int, Telemetry] = {}
        for anomaly in page.items:
            telemetry = uow.telemetry.by_id(anomaly.telemetry_id)
            if telemetry is not None:
                measurements[anomaly.telemetry_id] = telemetry
        return device, page, measurements


@router.get("", response_model=DeviceListDto)
async def list_devices(container: ContainerDep) -> DeviceListDto:
    rows = await run_in_db_thread(_list_devices, container)
    return DeviceListDto(
        items=[
            DeviceDto.from_domain(device, latest, anomaly)
            for device, latest, anomaly in rows
        ]
    )


@router.get("/{device_id}/latest", response_model=TelemetryDto)
async def latest_telemetry(container: ContainerDep, device_id: DeviceIdDep) -> TelemetryDto:
    device, telemetry, anomaly = await run_in_db_thread(_latest, container, device_id)
    if device is None:
        raise NotFoundError(f"unknown device: {device_id}")
    if telemetry is None:
        raise NotFoundError(f"no telemetry for device: {device_id}")
    return TelemetryDto.from_domain(telemetry, anomaly)


@router.get("/{device_id}/telemetry", response_model=TelemetryPageDto)
async def telemetry_history(
    container: ContainerDep, device_id: DeviceIdDep, query: HistoryQueryDep
) -> TelemetryPageDto:
    device, page, anomalies = await run_in_db_thread(
        _telemetry_page, container, device_id, query
    )
    if device is None:
        raise NotFoundError(f"unknown device: {device_id}")
    return TelemetryPageDto(
        items=[
            TelemetryDto.from_domain(item, _anomaly_for(anomalies, item))
            for item in page.items
        ],
        next_before_id=page.next_before_id,
    )


@router.get("/{device_id}/anomalies", response_model=AnomalyPageDto)
async def anomaly_history(
    container: ContainerDep, device_id: DeviceIdDep, query: HistoryQueryDep
) -> AnomalyPageDto:
    device, page, measurements = await run_in_db_thread(
        _anomaly_page, container, device_id, query
    )
    if device is None:
        raise NotFoundError(f"unknown device: {device_id}")
    return AnomalyPageDto(
        items=[
            AnomalyDto.from_domain(item, measurements[item.telemetry_id])
            for item in page.items
        ],
        next_before_id=page.next_before_id,
    )
