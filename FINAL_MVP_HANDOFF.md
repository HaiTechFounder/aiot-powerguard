# AIoT PowerGuard — Final MVP Handoff

Status: **REVIEW_REQUIRED** (integration re-verified 2026-09-25; not approved, not DONE).
ML promotion: **DATA_GATE_BLOCKED**. Hardware acceptance: **needs confirmation** — no hardware
evidence is stored in the repository (see NOT_RUN).

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
      │                                        └── InferenceEngine  (inference/loader.py)
      │                                              ├── UnavailableInference  ← default; every refusal
      │                                              ├── ShadowInference(IsolationForest) ← opt-in, scores + logs only
      │                                              └── IsolationForestInference ← active; validated artifact only
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
powershell -ExecutionPolicy Bypass -File scripts\setup_all.ps1   # venvs, npm ci, .env, migrations
.\START_POWERGUARD.cmd                                           # broker if Docker is up + backend + dashboard
powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1  # every offline gate
powershell -ExecutionPolicy Bypass -File scripts\stop_powerguard.ps1   # only if the launcher window was closed
```

`scripts/start_demo.ps1` (backend + dashboard + synthetic publisher) still works as before.

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
| Unusable ML artifact | Missing, corrupt, checksum-mismatched, wrong device, wrong regime, wrong feature version, legacy (no `expected_seq_stride`), non-UTF-8 metadata, undeserialisable model, or unvalidated → `unavailable`, never an exception into startup or ingestion. |
| Inference enabled but incomplete | Missing artifact dir / device / fingerprint → logged `unavailable`; `Settings` no longer refuses to start over it. |
| Shadow inference | Live telemetry is scored and would-flags are logged and counted; no anomaly row, no WS `anomaly` frame, `/health` stays `model: unavailable`. |
| Unknown / malformed device on WS | Close **4404** / **4400**; the dashboard stops retrying and says so. |
| Backend down | Dashboard shows an error state with a retry, keeps already-loaded readings. |

## Verified gates (run on this machine, 2026-09-25)

| Component | Gate | Result |
|---|---|---|
| Contracts | `scripts/check_contracts.py` | **PASS** — firmware ↔ MQTT ↔ backend ↔ REST/WS ↔ frontend ↔ ML adapter |
| Firmware | `pio run -e nodemcuv2` | **PASS** — RAM 40.3%, Flash 28.9% |
| Backend | `pytest` | **PASS** — 560 passed, 6 skipped |
| Backend | `ruff check .` / `mypy --strict src` | **PASS** — 42 files |
| Frontend | `typecheck` / `lint` / `build` | **PASS** — no chunk-size advisory (initial JS 186 kB; chart route 444 kB, loaded on demand) |
| Frontend | `test` + `test:coverage` | **PASS** — 233 tests, 95.46% statements / 87.65% branches, no `act(...)` warnings |
| ML | `pytest` / `ruff` / `mypy` (strict) | **PASS** — 187 tests, 13 files |
| ML | train + evaluate smoke | **PASS** — synthetic fixture; artifact records `expected_seq_stride: 2` |
| ML | `train --synthetic --promote` | **REFUSED as designed** — exit 3, `DATA_GATE_BLOCKED` |
| ML | `python -m powerguard_ml.gate` on `backend/data/powerguard.db` | **DATA_GATE_BLOCKED** (exit 3) — the correct result; no manifest, no attestation |
| Launcher | `scripts/test_launcher.ps1` under Windows PowerShell 5.1 | **PASS** — 44 checks: parse, control characters, encoding, ownership helpers |
| Launcher | live run `-NoBroker -NoBrowser`, second run, `stop_powerguard.ps1` | **PASS** — reported MQTT/model honestly, second run reused both without touching the run file, stop killed exactly the 2 owned processes, first window exited 1 |
| Hygiene | `git diff --check` + untracked-file whitespace scan | **PASS** |
| All offline gates | `scripts/verify_all.ps1` | **PASS** — 15/15; PowerShell 7 launcher check NOT_RUN (no `pwsh`) |
| Docs | markdown links and repo paths | **PASS** — all resolve |

### Integration defects found and fixed on 2026-09-25

1. **The online adapter could never score real firmware.** Training accepted a
   `seq` advance of `1..2`, but `adapter._contiguous` demanded exactly `+1`.
   Firmware reads every second and publishes every two, so every live reading
   reset the window. Contiguity is now one predicate, `preprocess.is_contiguous`,
   used by training, evaluation, the audit and the adapter; the stride flows
   manifest → training → artifact metadata → validation → adapter, is bounded to
   `1..8`, and a legacy artifact without it is refused rather than guessed.
2. **Training could fit on labelled faults.** Rows labelled abnormal were fed to
   the baseline. They are now held out.
3. **The gate passed without witnessed boots or labelled events**, counted
   abnormal rows toward the 1,000, and never compared the rows' device/regime to
   the manifest's. All four now block; template placeholders block too.
4. **An incomplete inference config crashed startup.** `Settings` raised when
   `INFERENCE_ENABLED=true` lacked a path/device/fingerprint. The loader now
   refuses it with a logged `unavailable`, and a last-resort handler turns any
   unanticipated ML exception into the same. Non-UTF-8 or absurdly nested
   metadata no longer escapes as a raw `UnicodeDecodeError`/`RecursionError`.
5. **`stop_powerguard.ps1` would refuse to stop the backend.** The run file was
   stamped after the health and dashboard waits, and the stop script ignored any
   process started more than 5 s before that stamp. Each entry now records its
   own process start time; identity is pid + start time.
6. **A second launcher run erased the first run's pids**, an unexpected child
   exit returned exit code 0 (so the `.cmd` window closed without showing why),
   any server on 5173 was "reused", and two hidden control characters (`\v`,
   `\b`) sat inside messages in `start_powerguard.ps1`. All fixed.
   `setup_all.ps1` now stops on a failed native step instead of printing
   "Setup complete".
7. **Live could be shown over a degraded backend or a future-stamped reading.**
   `evaluateLiveState` now also requires `status=ok`/`database=ready` and
   refuses a reading stamped beyond the threshold ahead of the browser clock.
8. **(final review) An unvalidated artifact could be activated.** With
   `INFERENCE_REQUIRE_VALIDATED=false` and `INFERENCE_MODE=active`, a synthetic
   artifact loaded as `active` and its verdicts would have been stored and
   broadcast. Activation now always requires `validated_real_data`; the relaxed
   flag allows shadow only.
9. **(final review) The headline chart said "Live Telemetry" over history.** It
   now follows the same live verdict as the badge and reads "Telemetry history"
   otherwise.

### Integration defects found and fixed on 2026-09-21

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
| Real ESP8266 device, end to end | **Needs confirmation.** Commit `ee45e11` ("Complete hardware E2E validation and MAX7219 display") adds display code and host tests only. No serial log, broker capture or results file is stored in the repo, so it is not treated as evidence. |
| Live MQTT broker session | Docker Engine was not running during this pass. |
| MQTT connect/disconnect loop in the old `logs/backend.err.log` | Not reproduced (no broker). Likely duplicate client ID — diagnosis only, not a verified fix. |
| `pio test -e native` (firmware host unit tests) | The host MinGW toolchain is under `C:\Users\Hai PC\...`; `ld` cannot link across the space. The **device build passes**. |
| Launcher checks under PowerShell 7 | `pwsh` is not installed; 5.1 passes. `verify_all.ps1` runs it automatically where `pwsh` exists. |
| Python 3.12 | Not installed; 3.11.8 is what ran. 3.12 remains canonical. |
| Any real-data ML training, evaluation, shadow run or promotion | No operator-approved, attested samples exist — `DATA_GATE_BLOCKED`. |
| Measured real-world recall / false-positive rate | Requires the above. |
| Hardware calibration (shunt, thresholds) | Requires hardware. |

## Known deferred work

- **No model is promoted (DATA_GATE_BLOCKED).** Inference wiring exists and is
  opt-in, shadow-by-default and fail-safe; what is missing is data: ≥1,000
  operator-approved normal samples from one attested regime, witnessed boot
  IDs, hardware provenance and labelled abnormal events. `python -m
  powerguard_ml.gate` prints the checklist. Nothing here waives it.
- The MQTT connect/disconnect loop in the old `logs/backend.err.log`
  (2026-09-23, 43 cycles in ~33 s) was **not reproduced** in this pass —
  Docker Engine was not running. Its pattern (connect → subscribe → unexpected
  disconnect about a second later) matches two backends sharing the fixed
  client ID `powerguard-backend-local` with a persistent session. That is a
  diagnosis from evidence, not a verified fix; the README now says to give a
  second backend its own `POWERGUARD_MQTT_INSTANCE_ID`.
- Anomaly pagination beyond the first page is not implemented (the UI says so).
- `socketRef` in `useDeviceStream.ts` is assigned but never read.
- React Router prints v7 future-flag notices in the view tests (informational;
  opting in changes routing behaviour and is left for the router upgrade).
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
   `boot_id`, a 2-second publish cadence, and `seq` advancing by 2 between
   published rows (one sample per second is read, one per two seconds is sent).
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
7. **Then, and only then, collect ML baseline data** by following
   `ml/REAL_DATA_GATE.md`: a witnessed capture, a review manifest, the gate,
   export, train with `--promote`, evaluate, shadow, and only then `active`.
   Changing the shunt or the current ceiling starts a new regime and
   invalidates any artifact trained under the old one.
8. **Record the evidence in the repo** — fill in `docs/hardware-test-results.md`
   (a template, every line `NOT_RUN` today), as `docs/hardware-test-checklist.md`
   describes. Until it records results, hardware status stays "needs
   confirmation" whatever a commit title says.

## Repository hygiene

`.env`, `.venv/`, `node_modules/`, `dist/`, `coverage/`, `data/`, `artifacts/`,
`*.joblib`, `*.db`, `.pio/`, `deploy/mosquitto/passwd` and
`firmware/include/secrets.h` are all ignored and confirmed unignorable-by-accident.
No credential appears in any tracked file; `.env.example` carries the
placeholder `change-me`, and `check-config` prints secrets as `<set>`.
