"""REST API v1 contract."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from alembic import command
from fastapi.testclient import TestClient

from powerguard.config import Settings
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.entities import AnomalyMethod, Telemetry
from powerguard.domain.services import FixedClock
from powerguard.main import create_app
from tests import builders
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE
from tests.conftest import alembic_config

OTHER_DEVICE = "powerguard-02"


@pytest.fixture
def seeded(
    settings: Settings, uow_factory: SqlUnitOfWorkFactory, clock: FixedClock
) -> dict[str, object]:
    """Two devices, five telemetry rows and one anomaly."""
    base = clock.now()
    ids: list[int] = []
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, base)
        uow.devices.upsert_from_telemetry(OTHER_DEVICE, FIRMWARE, BOOT_ID, base)
        for n in range(5):
            stored = uow.telemetry.insert_or_resolve_duplicate(
                builders.telemetry(seq=n, received_at=base + dt.timedelta(seconds=n))
            )
            assert isinstance(stored, Telemetry)
            assert stored.id is not None
            ids.append(stored.id)
        uow.anomalies.insert(
            builders.anomaly(
                telemetry_id=ids[-1],
                detected_at=base + dt.timedelta(seconds=4),
                method=AnomalyMethod.ISOLATION_FOREST,
            )
        )
        uow.commit()
    return {"base": base, "telemetry_ids": ids}


@pytest.fixture
def client(settings: Settings, seeded: dict[str, object]) -> Iterator[TestClient]:
    command.upgrade(alembic_config(settings.database_url), "head")
    with TestClient(create_app(settings)) as test_client:
        yield test_client
    del seeded


def test_list_devices_includes_latest_telemetry(client: TestClient) -> None:
    body = client.get("/api/v1/devices").json()
    assert [item["id"] for item in body["items"]] == [DEVICE_ID, OTHER_DEVICE]
    first = body["items"][0]
    assert first["status"] == "online"
    assert first["latest"]["seq"] == 4
    # Timestamps render as UTC RFC 3339 with a literal Z.
    assert first["last_seen_at"].endswith("Z")
    assert body["items"][1]["latest"] is None


def test_latest_telemetry_carries_its_anomaly(client: TestClient) -> None:
    response = client.get(f"/api/v1/devices/{DEVICE_ID}/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["seq"] == 4
    assert body["sensor_status"] == "ok"
    assert body["anomaly"]["method"] == "isolation_forest"
    assert body["anomaly"]["reasons"] == ["overcurrent_rule"]


def test_latest_is_404_for_unknown_device_and_for_no_telemetry(client: TestClient) -> None:
    unknown = client.get("/api/v1/devices/nobody/latest")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"

    empty = client.get(f"/api/v1/devices/{OTHER_DEVICE}/latest")
    assert empty.status_code == 404


def test_history_is_newest_first_and_paginates_stably(client: TestClient) -> None:
    first = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry?limit=2").json()
    assert [item["seq"] for item in first["items"]] == [4, 3]
    assert first["next_before_id"] is not None

    second = client.get(
        f"/api/v1/devices/{DEVICE_ID}/telemetry?limit=2&before_id={first['next_before_id']}"
    ).json()
    assert [item["seq"] for item in second["items"]] == [2, 1]

    last = client.get(
        f"/api/v1/devices/{DEVICE_ID}/telemetry?limit=2&before_id={second['next_before_id']}"
    ).json()
    assert [item["seq"] for item in last["items"]] == [0]
    assert last["next_before_id"] is None


def test_history_time_window(client: TestClient, seeded: dict[str, object]) -> None:
    base = seeded["base"]
    assert isinstance(base, dt.datetime)
    since = (base + dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    until = (base + dt.timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = client.get(
        f"/api/v1/devices/{DEVICE_ID}/telemetry?from={since}&to={until}"
    ).json()
    # from is inclusive, to is exclusive.
    assert [item["seq"] for item in body["items"]] == [2, 1]


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ("?from=2026-09-21T03:00:05Z&to=2026-09-21T03:00:01Z", "INVALID_QUERY"),
        ("?from=2026-09-21T03:00:00Z&to=2026-09-21T03:00:00Z", "INVALID_QUERY"),
        ("?from=not-a-date", "INVALID_QUERY"),
        ("?from=2026-09-21T03:00:00", "INVALID_QUERY"),
        ("?from=2026-09-21T03:00:00+07:00", "INVALID_QUERY"),
    ],
)
def test_invalid_time_windows_are_rejected(
    client: TestClient, query: str, expected_code: str
) -> None:
    response = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry{query}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.parametrize("query", ["?limit=0", "?limit=5001", "?before_id=0", "?before_id=-1"])
def test_out_of_range_query_parameters_are_422(client: TestClient, query: str) -> None:
    response = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_anomaly_history_includes_the_measurements(client: TestClient) -> None:
    body = client.get(f"/api/v1/devices/{DEVICE_ID}/anomalies").json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["method"] == "isolation_forest"
    assert item["telemetry_id"] is not None
    assert item["voltage_v"] == pytest.approx(7.84)
    assert item["current_a"] == pytest.approx(0.417)
    assert item["detected_at"].endswith("Z")


def test_history_for_unknown_device_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/devices/nobody/telemetry").status_code == 404
    assert client.get("/api/v1/devices/nobody/anomalies").status_code == 404


def test_malformed_device_id_is_a_query_error(client: TestClient) -> None:
    response = client.get("/api/v1/devices/NOT_VALID/latest")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_error_envelope_never_leaks_internals(client: TestClient) -> None:
    response = client.get("/api/v1/devices/nobody/latest")
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    text = response.text.lower()
    for leak in ("traceback", "sqlite", "select ", "password", "c:\\", "/users/"):
        assert leak not in text


def test_health_reports_subsystems_independently(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ready"
    # The broker is disabled in tests; that must not make the service unhealthy.
    assert body["mqtt"] == "disconnected"
    assert body["model"] == "unavailable"


# --- anomaly joins --------------------------------------------------------


def test_telemetry_history_carries_the_stored_anomaly(client: TestClient) -> None:
    """API_CONTRACT: the verdict travels with the reading it belongs to."""
    body = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry").json()
    by_seq = {item["seq"]: item for item in body["items"]}

    flagged = by_seq[4]["anomaly"]
    assert flagged is not None
    assert flagged["method"] == "isolation_forest"
    assert flagged["reasons"] == ["overcurrent_rule"]
    assert isinstance(flagged["id"], int)

    # Rows with no verdict report null, and there is exactly one verdict here.
    assert [seq for seq, item in by_seq.items() if item["anomaly"] is not None] == [4]


def test_device_list_latest_carries_the_stored_anomaly(client: TestClient) -> None:
    body = client.get("/api/v1/devices").json()
    latest = body["items"][0]["latest"]
    assert latest["seq"] == 4
    assert latest["anomaly"] is not None
    assert latest["anomaly"]["method"] == "isolation_forest"


def test_paginated_history_resolves_anomalies_on_every_page(client: TestClient) -> None:
    first = client.get(f"/api/v1/devices/{DEVICE_ID}/telemetry?limit=2").json()
    assert [item["seq"] for item in first["items"]] == [4, 3]
    assert first["items"][0]["anomaly"] is not None
    assert first["items"][1]["anomaly"] is None

    second = client.get(
        f"/api/v1/devices/{DEVICE_ID}/telemetry?limit=2&before_id={first['next_before_id']}"
    ).json()
    assert [item["seq"] for item in second["items"]] == [2, 1]
    assert all(item["anomaly"] is None for item in second["items"])


# --- published contract ---------------------------------------------------


def _schema(spec: dict[str, object], name: str) -> dict[str, object]:
    components: dict[str, object] = spec["components"]  # type: ignore[assignment]
    schemas: dict[str, object] = components["schemas"]  # type: ignore[index]
    return schemas[name]  # type: ignore[return-value,index]


def _is_nullable(schema: dict[str, object], field: str) -> bool:
    """True when the published schema lets this property be null."""
    properties: dict[str, object] = schema["properties"]  # type: ignore[assignment]
    definition: dict[str, object] = properties[field]  # type: ignore[assignment]
    variants = definition.get("anyOf")
    if isinstance(variants, list):
        return any(variant.get("type") == "null" for variant in variants)
    return definition.get("type") == "null"


def test_openapi_never_promises_a_null_persisted_id(client: TestClient) -> None:
    """Every row the API serves came from the database, so it has an id.

    A nullable id in the published contract would tell clients to handle a
    value they can never legitimately receive.
    """
    spec = client.get("/openapi.json").json()

    assert not _is_nullable(_schema(spec, "TelemetryDto"), "id")
    assert not _is_nullable(_schema(spec, "AnomalySummary"), "id")
    assert not _is_nullable(_schema(spec, "AnomalyDto"), "id")
    assert not _is_nullable(_schema(spec, "AnomalyDto"), "telemetry_id")


def test_openapi_anomaly_history_measurements_are_required(client: TestClient) -> None:
    """An anomaly cannot outlive its telemetry row, so its measurements exist."""
    spec = client.get("/openapi.json").json()
    anomaly = _schema(spec, "AnomalyDto")

    for field in ("voltage_v", "current_a", "power_w", "energy_wh"):
        assert not _is_nullable(anomaly, field)
        assert field in anomaly["required"]  # type: ignore[operator]


def test_openapi_keeps_genuinely_optional_fields_nullable(client: TestClient) -> None:
    """The tightening must not overreach: these really can be absent."""
    spec = client.get("/openapi.json").json()

    assert _is_nullable(_schema(spec, "TelemetryDto"), "sampled_at")
    assert _is_nullable(_schema(spec, "TelemetryDto"), "anomaly")
    assert _is_nullable(_schema(spec, "DeviceDto"), "latest")
    assert _is_nullable(_schema(spec, "AnomalySummary"), "score")
    assert _is_nullable(_schema(spec, "TelemetryPageDto"), "next_before_id")


def test_openapi_publishes_exactly_the_v1_read_endpoints(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {
        "/api/v1/health",
        "/api/v1/devices",
        "/api/v1/devices/{device_id}/latest",
        "/api/v1/devices/{device_id}/telemetry",
        "/api/v1/devices/{device_id}/anomalies",
    }
    for path, operations in spec["paths"].items():
        assert set(operations) == {"get"}, f"{path} exposes a mutating operation"
