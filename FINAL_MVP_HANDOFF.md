# AIoT PowerGuard — Final MVP Handoff

Status: **REVIEW_REQUIRED** (integration verified 2026-09-21; not approved, not DONE).

A battery-monitoring system in four parts: ESP8266 firmware, a Python ingestion
and API backend, a React dashboard, and an ML anomaly prototype. Everything
below was run on this machine unless it is listed under **NOT_RUN**.

## Architecture flow

```
ESP8266 + INA226
      │  JSON, schema_version 1, every 2 s
      │  powerguard/v1/devices/{id}/telemetry   (QoS 1)
      │  powerguard/v1/devices/{id}/status      (retained; LWT on drop)
      ▼
  MQTT broker (mosquitto — optional locally)
      │
      ▼
  Backend  ── validate (extra=forbid) → dedupe (boot_id, seq) → persist (SQLite/WAL)
      │                                        │
      │                                        └── InferenceEngine
      │                                              ├── UnavailableInference  ← wired today
      │                                              └── IsolationForestInference (ml/, ready, not wired)
      │
      ├── REST  /api/v1/{health,devices,devices/{id}/{latest,telemetry,anomalies}}
      └── WS    /ws/v1/devices/{id}   frames: telemetry | anomaly | status
      ▼
  Dashboard — REST seed (truth) + WS live tail, gap backfill on every open,
              600-point bounded buffer, honest "history may have a gap" banner
```

**REST is the truth; the socket is a live tail.** The dashboard seeds from REST,
then reconciles against REST on every socket open — the first included, because
readings can commit between the snapshot and the subscription.

## How to run

```powershell
pwsh -File scripts/setup_all.ps1     # venvs, npm ci, .env files, migrations
pwsh -File scripts/verify_all.ps1    # every offline gate
pwsh -File scripts/start_demo.ps1    # backend + dashboard, no broker needed
```

Dashboard on <http://127.0.0.1:5173>, API on <http://127.0.0.1:8000>.

With a local broker and live data:

```powershell
# one-time: backend/deploy/mosquitto/README.md creates deploy/mosquitto/passwd
$env:POWERGUARD_DEVICE_PASSWORD = "<the device password you created>"
pwsh -File scripts/start_demo.ps1 -WithBroker
```

Individual commands, if you prefer them to the scripts:

| Step | Command (from the component directory) |
|---|---|
| migrate | `.\.venv\Scripts\python.exe -m alembic upgrade head` |
| config check | `.\.venv\Scripts\python.exe -m powerguard check-config` (secrets print as `<set>`) |
| backend | `.\.venv\Scripts\python.exe -m powerguard serve` |
| dashboard | `npm run dev` |
| data, no broker | `.\.venv\Scripts\python.exe scripts\publish_synthetic.py --dry-run --scenario all` |
| data, live broker | `... publish_synthetic.py --host 127.0.0.1 --username powerguard-01 --password-env POWERGUARD_DEVICE_PASSWORD` |
| health | `curl http://127.0.0.1:8000/api/v1/health` |
| contracts | `python scripts/check_contracts.py` |
| firmware | `pio run -e nodemcuv2` |

Optional ML artifact workflow (the backend runs fine without it):

```powershell
cd ml
.\.venv\Scripts\python.exe -m powerguard_ml.train --synthetic 900 --artifacts artifacts
.\.venv\Scripts\python.exe -m powerguard_ml.evaluate --artifact artifacts/pg-synthetic-01/v1 --synthetic 600
```

### Graceful failure — verified, not assumed

| Condition | Observed behaviour |
|---|---|
| No broker | `/health` → `{"status":"ok","mqtt":"disconnected"}`. The API, database, REST and WS all work; a broker outage is a state, not an unhealthy service. |
| No model | `/health` → `"model":"unavailable"`. Ingestion and rules continue. The dashboard says *"Anomaly detection unavailable — this is not the same as no anomalies"* and still lists stored verdicts. |
| Unusable ML artifact | Missing, corrupt, checksum-mismatched, wrong device or wrong feature version → `unavailable`, never an exception into ingestion. |
| Unknown / malformed device on WS | Close **4404** / **4400**; the dashboard stops retrying and says so. |
| Backend down | Dashboard shows an error state with a retry, keeps already-loaded readings. |

## Verified gates (run on this machine, 2026-09-21)

| Component | Gate | Result |
|---|---|---|
| Contracts | `scripts/check_contracts.py` | **PASS** — firmware ↔ MQTT ↔ backend ↔ REST/WS ↔ frontend ↔ ML adapter |
| Firmware | `pio run -e nodemcuv2` | **PASS** — RAM 40.0%, Flash 28.7% |
| Backend | `pytest` | **PASS** — 529 passed, 6 skipped |
| Backend | `ruff check .` | **PASS** |
| Backend | `mypy --strict src` | **PASS** — 40 files |
| Frontend | `typecheck` / `lint` / `build` | **PASS** |
| Frontend | `test` + `test:coverage` | **PASS** — 197 tests, 96.77% statements / 88.65% branches |
| ML | `pytest` / `ruff` / `mypy` (strict) | **PASS** — 103 tests, 10 files |
| ML | train + evaluate smoke | **PASS** — on the deterministic synthetic fixture |
| Live API | `/health`, `/devices`, 404, CORS, WS close codes | **PASS** — against a running server |
| OpenAPI | live document vs committed fixture | **PASS** — paths, schema names and bodies identical |
| Hygiene | secrets / runtime artifacts | **PASS** — nothing ignorable would be committed; no hardcoded credentials |

### Integration defects found and fixed in this pass

1. **WebSocket rejections never reached the browser.** The backend closed
   *before* `accept()`, so a real ASGI server answered HTTP 403 and the browser
   saw close **1006** — indistinguishable from a network drop. The dashboard
   would have retried a nonexistent device forever. Starlette's `TestClient`
   reads the ASGI close message directly, so the existing tests reported 4404
   and passed while production did the opposite. Fixed by completing the
   handshake before sending the verdict, and covered by
   `tests/integration/test_websocket_real_server.py`, which runs uvicorn over a
   real socket because `TestClient` structurally cannot catch this.
2. **Clean-checkout migration failed.** `alembic upgrade head` — the first
   documented command — died with `unable to open database file` because
   nothing created the configured `data/` directory. The application engine did
   it, but migrations build their own engine and run first.
3. **The test suite read the developer's `.env`.** The README tells you to copy
   `.env.example` to `.env`; doing so then changed settings under test and broke
   `test_settings_without_a_password_yield_no_secrets`. Tests now pin
   `_env_file=None`, so a developer's local config cannot change results.

## NOT_RUN — honestly

| Item | Why |
|---|---|
| Real ESP8266 device, end to end | No hardware available. |
| Live MQTT broker session | Not started here; the broker is optional and Docker was not exercised. Ingestion is covered by the fake-transport suite and the publisher's dry run. |
| `pio test -e native` (firmware host unit tests) | The local g++ lives under `C:\Users\Hai PC\...`; `ld` cannot handle the space and fails to link. A toolchain on a space-free path is required. The **device build passes**. |
| Python 3.12 | Not installed; 3.11.8 is what ran. 3.12 remains canonical. |
| Any real-data ML training, evaluation or promotion | No operator-approved samples exist. |
| Measured real-world recall / false-positive rate | Requires the above. |
| Hardware calibration (shunt, thresholds) | Requires hardware. |

## Known deferred work

- **ML promotion is not wired.** `bootstrap.py` constructs
  `UnavailableInference()`. Swapping in `IsolationForestInference(<artifact dir>)`
  is the whole change; it is deliberately not made, because a model trained on
  synthetic data must not be presented as a detector. The adapter is proven
  compatible against the backend's real `Telemetry`, `AnomalyVerdict` and
  `InferenceEngine` in `ml/tests/test_backend_compatibility.py`.
- **≥1,000 operator-approved real samples** from one calibration regime remain
  the gate for production calibration and promotion. Nothing here waives it.
- Anomaly pagination beyond the first page is not implemented (the UI says so).
- Recharts vendor chunk exceeds Vite's 500 kB advisory.
- `socketRef` in `useDeviceStream.ts` is assigned but never read.
- `act(...)` warnings in the frontend hook tests — noise, not failures.
- README/traceability wording polish (carried from P04-T06).

## Exact next hardware validation steps

Work through `docs/hardware-test-checklist.md`; the ordering that matters:

1. **Bench the sensor before the software.** Confirm `SHUNT_RESISTANCE_OHM =
   0.01` and measure the real full-scale current. The INA226 ceiling
   (81.90 mV / 0.01 Ω = 8.19 A) is a *measurement* limit, not a safety
   threshold — do not relabel it as one.
2. **Flash and provision.** `cp firmware/include/secrets_example.h
   firmware/include/secrets.h`, fill in Wi-Fi and broker credentials
   (`secrets.h` is Git-ignored), then `pio run -e nodemcuv2 -t upload`.
3. **Watch the first boot.** `pio device monitor` at 115200. Confirm a stable
   `boot_id`, `seq` starting at 1 and incrementing, and a 2-second cadence.
4. **Confirm the wire format** against a real broker with
   `backend/scripts/check_broker.py` (it takes the password from an environment
   variable, never an argument). Payload bytes must match
   `publish_synthetic.py --dry-run` exactly.
5. **Confirm ingestion.** Start the backend with `POWERGUARD_MQTT_ENABLED=true`
   and verify: rows persist, duplicates by `(boot_id, seq)` are rejected, a
   reboot starts a new `boot_id`, and the retained `status` plus LWT drive
   online/offline.
6. **Set real measurement bounds.** Replace the
   `HARDWARE_CONFIGURATION_PENDING` values in `.env`
   (`POWERGUARD_MAX_ABS_CURRENT_A`, `POWERGUARD_MAX_ABS_POWER_W`) with
   hardware-validated limits. Warning and overcurrent policy is a separate
   decision and is still not configured here.
7. **Then, and only then, collect ML baseline data.** ≥1,000 operator-approved
   normal samples from one unchanged calibration regime, exported with
   `calibration_fingerprint` set on **every** row. Train, evaluate, read the
   report, and promote manually. Changing the shunt or the current ceiling
   starts a new regime and invalidates any artifact trained under the old one.

## Repository hygiene

`.env`, `.venv/`, `node_modules/`, `dist/`, `coverage/`, `data/`, `artifacts/`,
`*.joblib`, `*.db`, `.pio/`, `deploy/mosquitto/passwd` and
`firmware/include/secrets.h` are all ignored and confirmed unignorable-by-accident.
No credential appears in any tracked file; `.env.example` carries the
placeholder `change-me`, and `check-config` prints secrets as `<set>`.
