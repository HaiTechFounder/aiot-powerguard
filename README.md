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
| [`firmware/`](firmware/) | NodeMCU ESP8266 + INA226, PlatformIO | software complete; hardware acceptance NOT_RUN |
| [`backend/`](backend/) | FastAPI ingestion, REST v1, WebSocket v1, SQLite | complete |
| [`frontend/`](frontend/) | React 18 + TypeScript dashboard | complete |
| `ml/` | offline anomaly-detection training | not started — see below |

## Run it locally

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

Not implemented yet. The backend ships an `UnavailableInference` adapter that never flags, reports
`model: unavailable` through `/api/v1/health`, and the dashboard says "anomaly detection
unavailable" rather than "no anomalies".

Training needs at least 1,000 real, operator-approved samples from one hardware calibration regime.
No hardware has run yet, so that data does not exist; fitting a model to synthetic data would
produce an artifact that means nothing.

## What has not been run

Recorded honestly, and not claimed as passing:

- real hardware, end to end — no NodeMCU or INA226 available
  (checklist: [`docs/hardware-test-checklist.md`](docs/hardware-test-checklist.md))
- a live Mosquitto broker — none installed
- Python 3.12 — only 3.11.8 is installed here
- sustained write load and long-session behaviour

No cloud service, paid service or Docker installation is required for any of the above.
