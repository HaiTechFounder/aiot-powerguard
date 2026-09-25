# Real-data gate — status and the work still required

**Status: DATA_GATE_BLOCKED.** The integration is finished and tested. No model
is promoted, `POWERGUARD_INFERENCE_ENABLED` is `false`, and the dashboard
reports `detection unavailable` — which is the truth, not a placeholder.

One of the original five blockers is now **resolved** (the sequence stride).
Three remain, and all three need a person with the hardware.

## Resolved: the `seq` stride is 2 by design

Traced through the firmware rather than guessed:

| Fact | Source |
|---|---|
| Sensor samples every **1000 ms** | `POWERGUARD_SENSOR_SAMPLE_INTERVAL_MS`, `firmware/include/config.h` |
| Telemetry publishes every **2000 ms** | `POWERGUARD_TELEMETRY_INTERVAL_MS`, same file |
| `seq` is consumed on **every read attempt**, valid or not | `power_sensor.cpp`: `sample.seq = ++seq_;` |
| A publish carries only the **newest valid** sample, and skips a deadline if that sample was already sent | `main.cpp: enqueueTelemetry` |

Two samples per publish, one published, both sequence numbers consumed — so a
healthy stream advances `seq` by **2** per stored row. Nothing was missing.

`preprocess.EXPECTED_SEQ_STRIDE = 2` now encodes this, and contiguity requires
`1 <= advance <= stride`. The bound is what keeps it honest: a publish always
takes the newest sample, so any advance up to the stride is consistent with no
publish having been lost, while an advance **beyond** it proves a telemetry
deadline produced nothing — a genuine missing reading — and ends the segment.
Reboots and time gaps end a segment exactly as before. The stride is declared
per capture in the manifest, so a build that publishes every sample declares
`1` and an advance of 2 is a hole again. Firmware was not modified.

### One rule, offline and online

The rule is a single predicate, `preprocess.is_contiguous`, and every consumer
calls it with the same stride:

```
manifest.expected_seq_stride ──► train.py (every prepare call)
                              ──► artifact metadata.expected_seq_stride
                              ──► artifact.validate (1..8, integer, required)
                              ──► IsolationForestInference (live windows)
                              ──► evaluate.py, sqlite_source audit
```

Before this, the online adapter still demanded `seq == previous + 1`. Against
real firmware that reset the window on **every** reading, so a loaded model
could never have scored anything. The adapter now resets on exactly what
training cuts on: another `boot_id`, an advance outside `1..stride`, a time gap
that is zero or over twice the cadence, or a rejected reading.

- The stride is bounded to `1..8` (`MAX_SEQ_STRIDE`). An unbounded value would
  make every lost reading look contiguous.
- An artifact written before the stride was recorded is **refused** at load
  ("legacy artifact") rather than given a guessed stride. Retrain it. This
  includes any `ml/artifacts/pg-synthetic-01/v1` left over from an older run.
- Rows labelled abnormal (`label = 1`) are excluded from the fit; they exist to
  evaluate against, never to learn as normal.

Effect on the existing database: usable samples went from **60 to 3,171**, in 8
segments, longest 1,034. (An earlier count of 3,202 also accepted rows stored
at the *same* instant as contiguous; the shared predicate requires time to
advance, as training always did.)

## What the audit now finds

```powershell
.\backend\.venv\Scripts\python.exe -m powerguard_ml.sqlite_source `
  --database backend\data\powerguard.db --json ml\artifacts\real_data_audit.json
```

| Measure | Value |
|---|---|
| Stored readings | 3,212 |
| Devices / boots | 1 (`powerguard-01`) / 6 |
| Time range | 2026-09-21T15:03Z .. 2026-09-23T09:05Z |
| Off-state readings | 1,357 (42%) |
| Usable samples (30-sample contiguity, stride 2) | **3,171** in 8 segments |
| Stored anomalies (labelled events) | **0** |

Re-run on 2026-09-25 against the same database (read-only); the table above
is that run.

### The gate's own verdict

```powershell
.\backend\.venv\Scripts\python.exe -m powerguard_ml.gate --database backend\data\powerguard.db
```

prints `real-data gate: DATA_GATE_BLOCKED`, the reasons, and a numbered
checklist, and exits `3`. With `--manifest ml\review.json` it judges exactly
the rows that manifest approves. It never trains or writes a model.

### The three remaining blockers

1. **No calibration fingerprint exists anywhere.** The `telemetry` table has no
   such column, so the database cannot prove that any two rows came from the
   same electrical calibration. A missing fingerprint is never treated as a
   match — `gate.py` fails on it explicitly.
2. **No provenance is recorded.** Hardware and
   `backend/scripts/publish_synthetic.py` publish over one identical MQTT
   contract. No row already in this database can be certified as real device
   data, and **none of it may be relabelled as real retrospectively**. The
   capture below is what produces attestable data.
3. **Zero labelled abnormal events.** There is nothing to measure detection
   against, so no recall figure can be produced at all.

Also worth knowing before approving anything: boot `2dacfd19` (1,487 rows) has
a **median gap of 0.00 s** against a 2 s publish interval, and 78% of it is
off-state. That is not a live capture — it looks like a replay or backfill. The
audit now flags any boot whose median gap is far from the publish interval. Do
not approve that boot.

## The witnessed capture — 60–90 minutes on the ESP8266

### Before you start

- [ ] Note the firmware build actually flashed (`config.identity.firmwareVersion`).
- [ ] Note the shunt fitted — the design value is **0.01 Ω**. If yours differs,
      that is a different calibration regime.
- [ ] Choose a fingerprint string and keep it constant, e.g.
      `ina226-r010-fw0.1.0-bench-a`. It names the *measurement regime*: sensor,
      shunt, wiring, firmware.
- [ ] Have a stable load. Battery + resistive load is fine; what matters is that
      it stays on for the whole window.

### 1. Let the device reach the broker

The broker publishes on loopback by default, which a separate device cannot
reach. Bind it to the host's own LAN address for the capture — one named
interface, never all of them:

```powershell
$env:POWERGUARD_MQTT_BIND = "192.168.1.42"   # this host's LAN address
docker compose up -d mosquitto               # from backend/
```

Read `backend/deploy/mosquitto/README.md` first: it lists the firewall rule and
the conditions under which exposing an unencrypted broker is acceptable at all.
Unset the variable when you are finished.

### 2. Start the system and confirm the device is live

```powershell
.\START_POWERGUARD.cmd
```

Wait for `MQTT connected`, then watch the dashboard until the device shows
**Live** in green. If it shows `Stale data` or `Device offline`, fix that before
starting the clock — a capture with drop-outs mostly produces short segments.

### 3. Capture, and write down what you see

Let it run **60–90 minutes**, uninterrupted. At 2 s that is 1,800–2,700 rows,
which leaves margin after off-state and fragment losses for the 1,000 the gate
requires.

- Do not reboot, reflash or unplug the device.
- Do not change the load type mid-capture.
- Record the wall-clock **start and end** to the second, and the `boot_id`
  (visible in the dashboard's *Latest Reading* panel).

### 4. Induce and label abnormal events — separately

Detection cannot be evaluated without them. After the normal window,
deliberately cause the conditions you care about (overcurrent, brownout,
disconnect), noting each start and end to the second. Keep them **outside** the
approved normal intervals; the manifest rejects an overlap.

### 5. Write the review manifest

```powershell
.\backend\.venv\Scripts\python.exe -c "import json; from powerguard_ml import review, sqlite_source; rows = sqlite_source.read_telemetry(r'backend\data\powerguard.db', device_id='powerguard-01'); print(json.dumps(review.template('powerguard-01', rows), indent=2))" > ml\review.json
```

Edit `ml/review.json`:

| Field | What it must say |
|---|---|
| `provenance` | `hardware` — **only if you witnessed the capture** |
| `calibration_fingerprint` | the regime string you chose |
| `firmware_version` | the build actually flashed |
| `shunt_resistance_ohm` | the shunt actually fitted |
| `expected_seq_stride` | `2` for this firmware |
| `boot_ids` | only the boot(s) you witnessed — **required**; an empty list blocks |
| `approved_intervals` | the normal stretches, load on, no idling |
| `labelled_events` | the faults you induced, label `1` — **required**; none blocks |

Every field is an attestation. None can be recovered from the database later,
which is why it is written while the capture is fresh.

### 6. Check the gate, then export, train, evaluate

```powershell
.\backend\.venv\Scripts\python.exe -m powerguard_ml.gate `
  --database backend\data\powerguard.db --manifest ml\review.json
```

Fix every finding it prints before going on.

```powershell
.\backend\.venv\Scripts\python.exe -m powerguard_ml.sqlite_source `
  --database backend\data\powerguard.db --manifest ml\review.json `
  --export ml\data\approved.jsonl

.\backend\.venv\Scripts\python.exe -m powerguard_ml.train `
  --dataset ml\data\approved.jsonl --manifest ml\review.json `
  --artifacts ml\artifacts --promote
```

`--promote` exits `3` and prints `DATA_GATE_BLOCKED` unless every condition
holds. There is no override flag, by design.

### 7. Shadow before activating

```ini
POWERGUARD_INFERENCE_ENABLED=true
POWERGUARD_INFERENCE_ARTIFACT_DIR=../ml/artifacts/powerguard-01/v1
POWERGUARD_INFERENCE_DEVICE_ID=powerguard-01
POWERGUARD_INFERENCE_CALIBRATION_FINGERPRINT=<the regime you attested>
POWERGUARD_INFERENCE_MODE=shadow
```

Shadow scores live telemetry, logs `inference.shadow.would_flag`, and emits
nothing. Health keeps reporting `model: unavailable` on purpose: while every
verdict is discarded, `ready` would overstate what is running. Run it across the
load conditions you care about, read the counters, and only then set
`POWERGUARD_INFERENCE_MODE=active`.

`POWERGUARD_INFERENCE_ENABLED=false` disables AI and nothing else — ingestion,
MQTT, history, the API and the dashboard are untouched.

## What the gate refuses, and is tested to refuse

Synthetic or unknown provenance; `--promote` with `--synthetic` data (refused
before anything is generated); no manifest at all; a manifest still carrying a
`REPLACE-ME` placeholder; fewer than 1,000 approved **normal** samples (labelled
abnormal rows do not count); rows from a device or regime other than the one
the manifest names; two devices; two calibration regimes; any row without a
fingerprint; no witnessed `boot_ids`; a boot the manifest does not witness; no
labelled abnormal event, or none with a row inside it; a stride wider than the
one attested; a split too small to fit and calibrate.

At load time the backend additionally refuses a foreign device, a different
regime, a changed feature version or window, a missing or out-of-range
`expected_seq_stride`, malformed or non-UTF-8 metadata, a failed SHA-256, an
undeserialisable model, and any artifact not marked `validated_real_data`.
Every refusal — including an exception type nobody anticipated — becomes a
logged `model: unavailable`; none of them stops ingestion, REST, the WebSocket
or startup. Enabling inference without an artifact directory, device ID or
fingerprint is refused the same way rather than by crashing `Settings`.

## What has NOT been run

- Training or evaluation on approved real data.
- Any measurement of real-world recall or false-alert rate.
- A shadow run, or activation of a model, in a running backend.
- A hardware capture under a named calibration regime.
