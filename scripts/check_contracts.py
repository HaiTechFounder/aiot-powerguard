"""Cross-component contract check: firmware -> MQTT -> backend -> REST/WS -> frontend.

Four codebases in three languages agree on one wire format only by accident
unless something checks. Each component has its own tests, but nothing else
reads all four at once and asserts they still describe the same bytes.

This reads the *sources* -- the firmware's `snprintf` format strings, the
backend's Pydantic models and topic builders, the committed `openapi.json`, and
the frontend's TypeScript DTOs -- and compares them. It runs offline: no broker,
no hardware, no running backend.

    python scripts/check_contracts.py

Exit 0 when every component agrees, 1 when they do not, listing each drift.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware"
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

SCHEMA_VERSION = 1
TOPIC_PREFIX = "powerguard/v1/devices/"

#: What the firmware publishes, per MQTT_SPEC.md.
TELEMETRY_FIELDS = {
    "schema_version",
    "boot_id",
    "seq",
    "sampled_at",
    "voltage_v",
    "current_a",
    "power_w",
    "energy_wh",
    "sensor_status",
    "firmware_version",
}
STATUS_FIELDS = {"schema_version", "status", "boot_id", "firmware_version"}

#: The REST telemetry row the dashboard reads.
TELEMETRY_DTO_FIELDS = {
    "id",
    "device_id",
    "boot_id",
    "seq",
    "sampled_at",
    "received_at",
    "voltage_v",
    "current_a",
    "power_w",
    "energy_wh",
    "sensor_status",
    "anomaly",
}

WS_EVENT_TYPES = {"telemetry", "anomaly", "status"}

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def read(path: Path) -> str:
    if not path.exists():
        failures.append(f"missing file: {path.relative_to(ROOT)}")
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def json_keys(source: str) -> set[str]:
    """The `"key":` names inside a C string literal JSON template."""
    return set(re.findall(r'\\"([a-z_]+)\\":', source))


# -- 1. firmware -> MQTT ---------------------------------------------------


def check_firmware_payloads() -> None:
    telemetry = read(FIRMWARE / "src" / "telemetry.cpp")
    if not telemetry:
        return

    # The two telemetry branches differ only in `sampled_at`; both must carry
    # the full field set.
    templates = re.findall(r'"\{\\"schema_version\\":1,(.*?)\}"', telemetry, re.S)
    check(
        len(templates) >= 3,
        f"firmware: expected 3 JSON templates (2 telemetry + 1 status), found {len(templates)}",
    )
    for index, body in enumerate(templates):
        keys = json_keys('\\"schema_version\\":' + body)
        keys.add("schema_version")
        expected = STATUS_FIELDS if "status" in keys else TELEMETRY_FIELDS
        missing = expected - keys
        extra = keys - expected
        check(not missing, f"firmware template {index}: missing field(s) {sorted(missing)}")
        check(not extra, f"firmware template {index}: unexpected field(s) {sorted(extra)}")

    check(
        'sensor_status\\":\\"ok' in telemetry,
        "firmware: telemetry must publish sensor_status",
    )

    mqtt = read(FIRMWARE / "src" / "mqtt_manager.cpp")
    check(
        f'"{TOPIC_PREFIX}%s/telemetry"' in mqtt,
        f"firmware: telemetry topic is not {TOPIC_PREFIX}<id>/telemetry",
    )
    check(
        f'"{TOPIC_PREFIX}%s/status"' in mqtt,
        f"firmware: status topic is not {TOPIC_PREFIX}<id>/status",
    )


# -- 2. MQTT -> backend ingestion ------------------------------------------


def check_backend_ingestion() -> None:
    topics = read(BACKEND / "src" / "powerguard" / "mqtt" / "topics.py")
    check(
        f'"{TOPIC_PREFIX}+/telemetry"' in topics,
        "backend: telemetry subscription filter does not match the firmware topic",
    )
    check(
        f'"{TOPIC_PREFIX}+/status"' in topics,
        "backend: status subscription filter does not match the firmware topic",
    )

    schemas = read(BACKEND / "src" / "powerguard" / "mqtt" / "schemas.py")
    check(
        f"schema_version: Literal[{SCHEMA_VERSION}]" in schemas,
        f"backend: MQTT schema_version is not Literal[{SCHEMA_VERSION}]",
    )
    check(
        'extra="forbid"' in schemas,
        "backend: MQTT payloads must forbid unknown fields, or firmware drift goes unnoticed",
    )

    for field in sorted(TELEMETRY_FIELDS - {"schema_version"}):
        check(
            re.search(rf"^\s+{field}\s*:", schemas, re.M) is not None,
            f"backend: TelemetryPayloadV1 does not declare {field!r}",
        )


# -- 3. backend -> REST / WebSocket ----------------------------------------


def load_openapi() -> dict:
    # The committed fixture the frontend contract tests already pin against.
    candidates = sorted(FRONTEND.glob("tests/fixtures/openapi*.json")) + sorted(
        BACKEND.glob("**/openapi*.json")
    )
    for path in candidates:
        if "node_modules" in path.parts or ".venv" in path.parts:
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            failures.append(f"{path.relative_to(ROOT)}: not valid JSON")
            return {}
    failures.append("no committed openapi.json found for the REST contract check")
    return {}


def check_rest_contract(spec: dict) -> None:
    if not spec:
        return
    paths = spec.get("paths", {})
    required = {
        "/api/v1/health",
        "/api/v1/devices",
        "/api/v1/devices/{device_id}/latest",
        "/api/v1/devices/{device_id}/telemetry",
        "/api/v1/devices/{device_id}/anomalies",
    }
    missing = required - set(paths)
    check(not missing, f"openapi: missing endpoint(s) {sorted(missing)}")

    # The dashboard only ever reads. A mutating operation would be a contract
    # change nobody asked for.
    for route, operations in paths.items():
        for verb in operations:
            check(
                verb.lower() in {"get", "parameters"},
                f"openapi: {verb.upper()} {route} is a mutating operation",
            )

    schemas = spec.get("components", {}).get("schemas", {})
    telemetry = schemas.get("TelemetryDto") or schemas.get("TelemetryOut")
    if telemetry is None:
        failures.append("openapi: no TelemetryDto schema")
        return
    published = set(telemetry.get("properties", {}))
    missing_fields = TELEMETRY_DTO_FIELDS - published
    check(
        not missing_fields,
        f"openapi: TelemetryDto is missing {sorted(missing_fields)}",
    )


def check_websocket_contract() -> None:
    events = read(BACKEND / "src" / "powerguard" / "realtime" / "events.py")
    check("SCHEMA_VERSION" in events, "backend: realtime events publish no schema_version")
    for event in sorted(WS_EVENT_TYPES):
        check(
            f'"{event}"' in events,
            f"backend: realtime events do not emit a {event!r} frame",
        )


# -- 4. REST / WebSocket -> frontend ---------------------------------------


def check_frontend(spec: dict) -> None:
    contract = read(FRONTEND / "src" / "api" / "contract.ts")
    if not contract:
        return

    for field in sorted(TELEMETRY_DTO_FIELDS):
        check(
            re.search(rf"^\s+{field}[?]?:", contract, re.M) is not None,
            f"frontend: TelemetryDto does not declare {field!r}",
        )

    match = re.search(r"export type EventType =([^;]+);", contract)
    check(match is not None, "frontend: no EventType union")
    if match:
        declared = set(re.findall(r'"(\w+)"', match.group(1)))
        check(
            declared == WS_EVENT_TYPES,
            f"frontend: EventType is {sorted(declared)}, backend emits {sorted(WS_EVENT_TYPES)}",
        )

    socket = read(FRONTEND / "src" / "realtime" / "socket.ts")
    check(
        "schema_version !== 1" in socket,
        "frontend: the socket does not pin schema_version to 1",
    )
    check(
        "/ws/v1/devices/" in socket,
        "frontend: the socket does not use the /ws/v1/devices/ path",
    )

    client = read(FRONTEND / "src" / "api" / "client.ts")
    if spec:
        for route in spec.get("paths", {}):
            stem = route.replace("{device_id}", "")
            check(
                stem.split("/")[-1] == "" or stem.rstrip("/").split("/")[-1] in client
                or route in client,
                f"frontend: no client call for {route}",
            )


# -- 5. ML adapter -> backend inference port -------------------------------


def check_ml_adapter() -> None:
    ports = read(BACKEND / "src" / "powerguard" / "domain" / "ports.py")
    adapter = read(ROOT / "ml" / "src" / "powerguard_ml" / "adapter.py")
    if not adapter:
        return
    check(
        "def readiness(self) -> str:" in ports,
        "backend: InferenceEngine no longer declares readiness()",
    )
    check(
        "def readiness(self) -> str:" in adapter,
        "ml: adapter does not implement readiness()",
    )
    check(
        "def evaluate(self, telemetry" in adapter,
        "ml: adapter does not implement evaluate(telemetry)",
    )
    # `unavailable` is the value the health endpoint publishes; the ML adapter
    # must degrade to exactly that, not to a third state.
    check(
        'UNAVAILABLE = "unavailable"' in adapter and 'READY = "ready"' in adapter,
        "ml: adapter readiness vocabulary does not match the backend's",
    )
    unavailable = read(BACKEND / "src" / "powerguard" / "inference" / "unavailable.py")
    check(
        '"unavailable"' in unavailable,
        "backend: UnavailableInference no longer reports 'unavailable'",
    )


def main() -> int:
    check_firmware_payloads()
    check_backend_ingestion()
    spec = load_openapi()
    check_rest_contract(spec)
    check_websocket_contract()
    check_frontend(spec)
    check_ml_adapter()

    if failures:
        print(f"{len(failures)} contract drift(s):\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("contracts agree: firmware -> MQTT -> backend -> REST/WS -> frontend, and ML adapter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
