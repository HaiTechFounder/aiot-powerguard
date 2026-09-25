# aiot-powerguard

AIoT smart power monitoring system with real-time telemetry and AI-based anomaly detection.

A NodeMCU ESP8266 with an INA226 measures total-system DC voltage, current and power, publishes it
over MQTT, and a local FastAPI backend validates, deduplicates and persists it to SQLite before
serving a REST API, a WebSocket stream and a React dashboard.

```
INA226 → ESP8266 → Mosquitto → FastAPI → SQLite → React dashboard
                                   └──── WebSocket ────┘
```

## Components

| Directory | What it is | State |
|---|---|---|
| [`firmware/`](firmware/) | NodeMCU ESP8266 + INA226 (+ MAX7219 readout), PlatformIO | device build passes; hardware acceptance **needs confirmation** — no hardware evidence is stored in the repo |
| [`backend/`](backend/) | FastAPI ingestion, REST v1, WebSocket v1, SQLite; opt-in inference wiring | complete |
| [`frontend/`](frontend/) | React 18 + TypeScript dashboard | complete |
| [`ml/`](ml/) | anomaly pipeline, artifact format, real-data gate | prototype complete; **DATA_GATE_BLOCKED** — no model is promoted |

## Start it (one command)

First time only, to install everything:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_all.ps1
```

After that, **double-click `START_POWERGUARD.cmd`** in this folder - or run it
from any shell:

```powershell
.\START_POWERGUARD.cmd
```

It starts the Mosquitto broker (if Docker Desktop is running), the backend and
the dashboard, waits for each one to answer, then opens
<http://127.0.0.1:5173>. Keep the window open; Ctrl+C stops everything it
started. If the window was closed instead:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop_powerguard.ps1
```

Useful switches: `-NoBroker` (skip Docker), `-NoBrowser`, `-RequireBroker`
(fail rather than run without MQTT).

Things worth knowing:

- **Docker Desktop is optional.** Without it the dashboard still runs and
  honestly reports `MQTT disconnected` - no device telemetry can arrive, and
  the UI says so rather than pretending otherwise.
- **Nothing is installed on each start.** Missing dependencies produce the
  exact command to run instead.
- **Ports are never seized.** If something else holds 8000 or 5173, the script
  names the conflict and stops; it does not kill the other program. A backend
  or dashboard that is already PowerGuard's own is reused, not duplicated.
- **It only stops what it started.** `logs/powerguard-run.json` records each
  process it launched with that process's start time; `stop_powerguard.ps1`
  leaves alone any pid Windows has since recycled, and a second launcher run
  adds to the file instead of erasing the first run's entries.
- **Credentials stay in `backend/.env`** (Git-ignored). They are never printed,
  logged or passed on a command line.
- **Run one backend per broker.** Every backend connects as
  `powerguard-backend-${POWERGUARD_MQTT_INSTANCE_ID}` (default `local`) with a
  persistent session, so two backends on one broker evict each other in a
  roughly one-second connect/disconnect loop. Give a second one its own
  `POWERGUARD_MQTT_INSTANCE_ID`.
- Logs land in `logs/`, which is Git-ignored.
- `scripts\test_launcher.ps1` checks the launcher offline: every script parses,
  no hidden control characters, encoding, and the ownership helpers.

## Run it locally (manual, two shells)

Two shells. Neither needs hardware or a broker.

```powershell
# backend
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m powerguard serve        # http://127.0.0.1:8000

# dashboard
cd frontend
npm ci
npm run dev                                           # http://127.0.0.1:5173
```

Details, including the synthetic publisher that stands in for a device, are in
[`backend/README.md`](backend/README.md) and [`frontend/README.md`](frontend/README.md).

## Anomaly detection

The pipeline exists; a promoted model does not. **Status: DATA_GATE_BLOCKED.**

- `ml/` holds the feature contract, an IsolationForest baseline, a checksum-verified artifact format,
  a backend adapter and the real-data gate. See [`ml/README.md`](ml/README.md) and
  [`ml/REAL_DATA_GATE.md`](ml/REAL_DATA_GATE.md).
- The backend wires inference through `inference/loader.py`, and it is **opt-in**
  (`POWERGUARD_INFERENCE_ENABLED=false` by default). Disabled, missing, misconfigured, legacy,
  synthetic, foreign-device, wrong-regime or tampered artifacts all degrade to
  `UnavailableInference` with a logged reason — never a failed startup.
- Enabled with a validated artifact it runs in **shadow** by default: live telemetry is scored,
  would-be verdicts are logged and counted, and nothing is stored or broadcast. `/api/v1/health`
  keeps reporting `model: unavailable` in shadow, because nothing it decides reaches the system.
- The dashboard says "anomaly detection unavailable", never "no anomalies".

Promotion needs at least 1,000 operator-approved normal samples from one attested calibration
regime, witnessed boot IDs, hardware provenance attested by a person, and labelled abnormal events.
None of that exists yet, so no artifact is `validated_real_data`. To see exactly what is missing:

```powershell
.\backend\.venv\Scripts\python.exe -m powerguard_ml.gate --database backend\data\powerguard.db
```

It prints `DATA_GATE_BLOCKED`, the reasons and an operator checklist, and exits `3`. Synthetic data
can never be promoted: `train --synthetic ... --promote` is refused outright.

## What has not been run

Recorded honestly, and not claimed as passing:

- real hardware, end to end — **needs confirmation.** Commit `ee45e11` is titled "Complete hardware
  E2E validation and MAX7219 display", but it adds firmware display code and host tests only; no
  serial log, broker capture or results file is stored in the repository, and
  [`docs/hardware-test-checklist.md`](docs/hardware-test-checklist.md) has not been filled in.
  The local database holds readings whose 2-second cadence and `seq` stride of 2 are consistent
  with the firmware, but it records no provenance, so they cannot be certified as hardware data.
- a live Mosquitto broker in this verification pass — Docker Engine was not running
- `pio test -e native` — the host MinGW toolchain lives under a path with a space and `ld` cannot
  link there (the ESP8266 device build does pass)
- PowerShell 7 parsing of the launcher scripts — `pwsh` is not installed here; Windows
  PowerShell 5.1 parsing is checked by `scripts/test_launcher.ps1`
- Python 3.12 — only 3.11.8 is installed here
- any real-data ML training, evaluation, shadow run or promotion
- sustained write load and long-session behaviour

No cloud service, paid service or Docker installation is required for any of the above.
