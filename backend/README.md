# AIoT PowerGuard — Backend

FastAPI process that ingests MQTT v1 telemetry, persists it to SQLite, serves a read-only REST API
and streams live events over WebSocket.

```
device --MQTT v1--> Mosquitto --> ingestion --> SQLite
                                      |
                                      +--> WebSocket /ws/v1
                                      +--> REST /api/v1
```

One process is the sole database writer (ADR-001, ADR-003). There is no mutation or authentication
API in the local MVP.

## Status

| Area | State |
|---|---|
| Configuration, schema, repositories | implemented |
| MQTT validation and ingestion | implemented |
| REST v1 and WebSocket v1 | implemented |
| Anomaly detection | port only — `UnavailableInference` never flags |
| Live Mosquitto run | **NOT_RUN** (no broker available yet) |
| Real device end to end | **NOT_RUN** (no hardware — see [../docs/hardware-test-checklist.md](../docs/hardware-test-checklist.md)) |

## Setup

Python 3.12 is canonical; 3.11 is supported and is what this repository was developed on.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
Copy-Item .env.example .env        # then edit it
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m powerguard serve
```

`.env` is Git-ignored.

Three lock files, all exact:

| File | Holds |
|---|---|
| `requirements.lock` | runtime transitive pins |
| `requirements-dev.lock` | runtime plus tooling |
| `requirements-build.lock` | the build backend, mirroring `[build-system].requires` |

Regenerate the first two with `python -m piptools compile --strip-extras pyproject.toml`
(add `--extra=dev` for the dev lock). `requirements-build.lock` is maintained by hand, because
pip-compile classifies `setuptools` as unsafe and refuses to emit it; change it and
`pyproject.toml` together. `--no-build-isolation` installs against those pinned build tools instead
of letting pip resolve its own copies, which is what makes the editable install reproducible.

Python 3.12 is canonical. Python 3.11.8 is supported and is what this repository
was developed on; **Python 3.12 is NOT_RUN here** because that interpreter is not installed. Docker
is optional. No cloud or paid service is required at any point.

## Quick start, end to end

From a clean checkout, in PowerShell, with no broker and no hardware:

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .

Copy-Item .env.example .env          # then edit; .env is Git-ignored
.\.venv\Scripts\python.exe -m powerguard check-config   # secrets show as <set>

.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m powerguard check-db       # wal / foreign_keys / revision

.\.venv\Scripts\python.exe -m pytest                    # the mandatory software gate
.\.venv\Scripts\python.exe -m powerguard serve
```

Then, in a second shell, see data without a device:

```powershell
.\.venv\Scripts\python.exe scripts\publish_synthetic.py --dry-run --scenario all --count 3
curl http://127.0.0.1:8000/api/v1/devices
```

Set `POWERGUARD_MQTT_ENABLED=false` in `.env` to run the API alone, with no broker at all.

## Commands

| Command | Purpose |
|---|---|
| `python -m powerguard serve` | run the API and MQTT ingestion |
| `python -m powerguard check-config` | validate configuration, print a redacted summary |
| `python -m powerguard check-db` | report journal mode, pragmas and the current migration |
| `python -m alembic upgrade head` | apply migrations (startup never auto-migrates) |
| `python -m alembic downgrade -1` | roll one migration back |
| `python -m pytest` | full test suite |
| `python -m pytest --cov=powerguard` | suite with a coverage report |
| `python -m ruff check .` | lint |
| `python -m mypy src` | strict type check |
| `python scripts/publish_synthetic.py --list-scenarios` | the synthetic device's scenarios |

## Configuration

Every setting uses the `POWERGUARD_` prefix and is validated at startup; see
[`.env.example`](.env.example) for the full list. Invalid values fail fast with a message that never
contains a secret. `MQTT_USERNAME` and `MQTT_PASSWORD` are required whenever `MQTT_ENABLED` is true.

`MIN_VOLTAGE_V`, `MAX_VOLTAGE_V`, `MAX_ABS_CURRENT_A`, `MAX_ABS_POWER_W` and `POWER_REL_TOLERANCE`
are **protocol-integrity bounds**, not safety thresholds. The shipped values are development
placeholders derived from the INA226 measurement ceiling and are marked
`HARDWARE_CONFIGURATION_PENDING`; replace them with hardware-validated bounds before hardware
acceptance, and never relabel them as warning or overcurrent settings.

## API

| Method | Path |
|---|---|
| GET | `/api/v1/health` |
| GET | `/api/v1/devices` |
| GET | `/api/v1/devices/{device_id}/latest` |
| GET | `/api/v1/devices/{device_id}/telemetry` |
| GET | `/api/v1/devices/{device_id}/anomalies` |
| WS | `/ws/v1/devices/{device_id}` |

History endpoints accept `from` (inclusive), `to` (exclusive), `limit` (1..5000, default 500) and
`before_id`. Results are newest first; `next_before_id` is set only when another page exists.

Errors always use one envelope:

```json
{"error": {"code": "INVALID_QUERY", "message": "to must be later than from", "details": null}}
```

400 for cross-field query errors, 404 for missing resources, 422 for parameter validation, 500 for
anything unexpected — with a correlation id in the response and the detail only in the log.

WebSocket frames are v1 envelopes:

```json
{"schema_version": 1, "type": "telemetry", "emitted_at": "2026-09-21T03:00:00.000Z", "data": {}}
```

`type` is `telemetry`, `anomaly` or `status`. Unknown device closes 4404, malformed id 4400, and a
client that cannot keep up is dropped with 1013 without affecting anyone else. Delivery is best
effort; the REST API over the database is the recovery path after a reconnect.

## Ingestion rules

| Outcome | Persisted | Broadcast | MQTT ack |
|---|---|---|---|
| Accepted telemetry | yes | after commit | yes |
| Duplicate `(device_id, boot_id, seq)` | no | no | yes |
| Accepted status | device row | after commit | yes |
| Invalid topic or payload | no | no | yes — prevents a poison loop |
| Transient storage failure | no | no | **no** — broker redelivers |

`received_at` is server UTC captured in the network callback; `sampled_at` is diagnostic only and is
currently always `null` because the firmware has no time source yet. Sequence gaps are counted, never
backfilled. Energy is never summed across boots.

## Verification without hardware

The mandatory path injects a fake MQTT *transport* — the handful of Paho calls the adapter makes —
so the production adapter itself runs under test: CONNACK handling, subscription results, the
bounded ingress queue, acknowledgement timing, saturation, redelivery and shutdown ordering. The
real lifespan, real validation, real repositories and a real migrated database are all in the path;
only the broker is absent.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration
```

`tests/integration/test_lifespan.py` runs the whole process end to end: one delivery becomes a
persisted row that the REST API serves, and the acknowledgement is asserted on the transport.

## Synthetic publisher

`scripts/publish_synthetic.py` stands in for the firmware. Valid payloads are byte-accurate v1
documents inside the hardware contract (2S pack, 8.4 V ceiling), so they exercise the real contract.

| Scenario | What it drives |
|---|---|
| `normal` | steady discharge, sequence increasing by one |
| `duplicate` | an identical resend, for QoS 1 idempotency |
| `spike` | a brief current surge, still inside the contract |
| `drift` | voltage sagging across the run |
| `gap` | skipped sequence numbers |
| `out-of-order` | a later sequence delivered before an earlier one |
| `malformed` | payloads the backend must reject and acknowledge |
| `status` | online, telemetry, then a **graceful** offline |
| `reconnect` | an **unexpected drop** so the broker publishes the will, then a reboot |

```powershell
# print the whole plan, publish nothing, no broker needed
.\.venv\Scripts\python.exe scripts\publish_synthetic.py --dry-run --scenario all --count 3

# reproducible: same arguments plus the same seed give an identical plan
.\.venv\Scripts\python.exe scripts\publish_synthetic.py --dry-run --scenario gap --seed 7

# against a real broker; the password comes from the environment, never a flag
$env:POWERGUARD_DEVICE_PASSWORD = "<password>"
.\.venv\Scripts\python.exe scripts\publish_synthetic.py --username powerguard-device --scenario duplicate
```

### Plan and execution

The run is planned first and executed second, and only the second half touches a broker or a clock.

**Planning is pure.** The same arguments and the same `--seed` produce an identical plan — sequence
numbers, measurements, boot ids, topics, scenario order and payload bytes included. Nothing reads
the wall clock, a UUID or the global random state, so two plans can be compared byte for byte.
`--with-timestamp` therefore emits a *logical* `sampled_at`, derived from a fixed epoch and
`--interval`; MQTT_SPEC only asks for a UTC RFC 3339 value with milliseconds.

**Execution** walks the plan, waits for each QoS 1 publication to be confirmed by the broker, and
performs the one step a payload cannot express.

### Sessions

A run is a sequence of **sessions**. A session is one connection: one `boot_id`, one registered
will, and every message published while it holds that connection — its telemetry, its status
announcements, and the graceful offline that ends it.

A drop ends a session. The reconnect that follows establishes a **new** one, with a new boot id and
a new will registered *before* the connection is made, so the broker never holds a will describing a
session that has already died. Nothing from a finished session is reused afterwards: later scenarios
in the same run continue in the replacement session, and the final graceful offline always carries
the boot id that actually holds the connection.

A dry run prints each session's header and will, and the offline that would be sent at exit.

The publisher is the only thing that reconnects: Paho's automatic reconnect is switched off
(`reconnect_on_failure=False`), so its retry can never race a deliberate drop.

### Ending a session: two different things

| | What happens | Scenario |
|---|---|---|
| **Graceful offline** | the device publishes `status: offline` at QoS 1, **waits for the broker to confirm it**, then disconnects | `status`, and the end of any normal run |
| **Unexpected drop** | the device vanishes without a DISCONNECT; the **broker** publishes the will it holds for that session | `reconnect` |

The publisher never hand-publishes a will payload and calls it an LWT test: that would prove nothing
about the will the broker actually holds. In a dry run the drop is printed as a `DROP` line showing
what the broker is expected to publish.

A graceful offline that is not confirmed within the timeout is reported on stderr and the command
**exits non-zero**. The same is true of any unconfirmed telemetry publication. A goodbye the broker
did not acknowledge is never reported as one.

The device id must match the MQTT_SPEC grammar and the publisher refuses to address any topic
outside that device's own v1 namespace. A run is bounded whatever `--count` says.

## Optional broker smoke test

The mandatory gate needs no broker. When one is available, one authenticated smoke test proves the
parts a fake cannot: a real CONNACK, a real SUBACK, real QoS 1 delivery, a retained LWT, and a
WebSocket frame that could only have come from a message that really crossed the broker.

Two broker paths, both documented in [`deploy/mosquitto/README.md`](deploy/mosquitto/README.md):

```powershell
# native Windows, from the backend directory
mosquitto -c deploy\mosquitto\mosquitto.native.conf -v

# or Docker — create deploy/mosquitto/passwd first, it is mounted read-only
docker compose up -d mosquitto
```

Only MQTT on port 1883 is served; no WebSocket listener is configured on the broker.

Check that it accepts your credentials and both v1 filters — the password is read from the
environment, never passed as an argument where a process listing would show it:

```powershell
$env:POWERGUARD_BROKER_PASSWORD = "<password>"
.\.venv\Scripts\python.exe scripts\check_broker.py --username powerguard-backend
```

Then:

```powershell
$env:POWERGUARD_SMOKE_BROKER = "1"
$env:POWERGUARD_SMOKE_USERNAME = "powerguard-backend"
$env:POWERGUARD_SMOKE_PASSWORD = "<the password you created>"
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mosquitto_smoke.py -m integration
```

Without a broker it skips with that command in the skip message, and the result is recorded as
NOT_RUN — never as a pass.

## Known NOT_RUN and current limitations

| Item | Status |
|---|---|
| Live Mosquitto smoke | **NOT_RUN** — no broker installed; enabling command above |
| Real CONNACK/SUBACK, QoS 1 redelivery timing, retained LWT from a real device | **NOT_RUN** |
| Real device over Wi-Fi, end to end | **NOT_RUN** — see [../docs/hardware-test-checklist.md](../docs/hardware-test-checklist.md) |
| Python 3.12 | **NOT_RUN** — only 3.11.8 is installed here |
| Sustained write load / SQLite contention | **NOT_RUN** |

Limitations by design in this phase: one process is the sole database writer; anomaly detection is a
port only (`UnavailableInference` never flags); there is no authentication, no mutation endpoint and
no TLS, so the port must not be exposed beyond localhost or a trusted LAN; WebSocket delivery is best
effort with REST over the database as the recovery path.

## Module map

| Package | Responsibility |
|---|---|
| `config` | typed settings, validation, redaction |
| `bootstrap` | composition root, migration check, background tasks |
| `db` | engine and pragmas, ORM models, repositories, unit of work |
| `domain` | entities, ports, errors — standard library only |
| `mqtt` | topics, strict payload models, validation, ingestion, Paho adapter |
| `realtime` | event envelopes and the bounded fan-out hub |
| `api` | error envelope, DTOs, dependencies, routes |
| `inference` | port plus the unavailable adapter |

Dependency direction is adapters → domain. ORM rows never leave `db`; Pydantic models are adapter
DTOs, not domain entities.
