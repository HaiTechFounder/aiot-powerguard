# PowerGuard ML — anomaly prototype (Phase 05 Lite)

A working anomaly-detection pipeline: dataset schema, preprocessing, the
`powerguard_features_v1` feature contract, one baseline model, train/evaluate
CLIs, a verified artifact format, and an adapter the backend can use as its
`InferenceEngine`.

## What this is not

**It is not a calibrated detector, and no number it prints is a claim about
real-world accuracy.**

The bundled dataset is synthetic and deterministic. It exists to verify
*software mechanics* — that a window is built correctly, a model fits, an
artifact round-trips, an adapter answers the backend's port. Any recall or
flag rate measured on it is a statement about the generator, not about a real
load. Every report prints `data_quality: quality_not_established` for this
reason.

**Production calibration, evaluation and promotion remain DEFERRED** until at
least 1,000 operator-approved real samples from a single calibration regime
exist. Finishing this prototype does not waive that gate.

## Setup

```powershell
cd ml
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Train

```powershell
# Deterministic synthetic fixture — verification only.
.\.venv\Scripts\python.exe -m powerguard_ml.train --synthetic 900 --artifacts artifacts

# A real export (.jsonl or .csv).
.\.venv\Scripts\python.exe -m powerguard_ml.train --dataset data/export.jsonl --artifacts artifacts
```

Writes `artifacts/{device_id}/{model_version}/` containing `model.joblib` and
`metadata.json`. Both are Git-ignored: models are runtime data.

## Evaluate

```powershell
.\.venv\Scripts\python.exe -m powerguard_ml.evaluate `
  --artifact artifacts/pg-synthetic-01/v1 --synthetic 600 --json artifacts/report.json
```

Reports sample counts, segment and window counts, time bounds, threshold,
flagged windows, per-scenario behaviour on injected departures, and score
quantiles — plus the caveat above, in the output rather than in a footnote.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest        # 71 tests
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy          # strict
```

## Dataset schema

`.jsonl` or `.csv`, one row per reading. Required: `id`, `device_id`,
`boot_id`, `seq`, `received_at` (RFC 3339 with an offset), `voltage_v`,
`current_a`, `power_w`, `energy_wh`, `sensor_status`. Optional: `label`
(operator ground truth, `0` or `1`) and `calibration_fingerprint`.

Parsing rejects rather than coerces — a malformed row names itself and its line
number instead of becoming a silent zero.

## Preprocessing

One device and one calibration regime per dataset; mixing either is refused,
because an artifact trained across two electrical calibrations was fitted on
two different sensors. Rows with `sensor_status != "ok"` or non-finite values
are dropped, duplicates by `id` are dropped, and rows are ordered by
`received_at` with `id` as the tie-break — the backend's own order (ADR-006).

The remainder is cut into **contiguous segments**. A reboot, a sequence hole,
or a time gap over twice the 2-second cadence ends a segment, and a segment
shorter than one window is dropped rather than padded. The train/calibration
split is by time: every training row precedes every calibration row.

## Features — `powerguard_features_v1`

Thirteen values from a trailing 30-sample window, in this exact order:

`voltage_v`, `current_a`, `power_w`, `delta_voltage_v`, `delta_current_a`,
`delta_power_w`, `mean_voltage_v`, `mean_current_a`, `mean_power_w`,
`std_voltage_v`, `std_current_a`, `std_power_w`, `power_residual`
(`abs(power_w - voltage_v * current_a)`).

Cumulative `energy_wh` is deliberately excluded: it only increases, so a model
trained on it learns the clock rather than the load. Order, units, window and
formulas are immutable metadata — changing any of them means a new
`FEATURE_VERSION`, and existing artifacts are then refused.

## Model

`RobustScaler` → `IsolationForest(n_estimators=200, random_state=42, n_jobs=1)`,
exactly as `ML_ARCHITECTURE.md` specifies. No hyperparameter search.

Raw `-score_samples` values have no interpretable units, so they are never what
a threshold is applied to. The held-out calibration split supplies a
distribution, and an online score is the raw value's empirical percentile
within it, in `0..1`. Default threshold `0.99`. A verdict requires **two
consecutive** above-threshold windows: at a 2-second cadence one extreme window
is noise.

## Artifact

`artifacts/{device_id}/{model_version}/model.joblib` plus `metadata.json`
carrying the model version, device, creation time, feature version and order,
window, cadence, threshold, consecutive count, calibration score distribution,
training interval and counts, library versions, shunt resistance, calibration
fingerprint, `data_quality`, `data_provenance`, and the SHA-256 of
`model.joblib`.

`joblib.load` executes what it unpickles, so an artifact is code, not a
document. Loading validates metadata **before** opening the pickle and refuses
on: an unknown metadata version, a different feature version or feature order,
a different window, a foreign device, a different calibration regime, an empty
calibration distribution, a missing file, or a checksum mismatch. Nothing here
downloads an artifact.

## Backend compatibility

`powerguard_ml.adapter.IsolationForestInference` implements the backend's
`InferenceEngine` port: `readiness() -> "ready" | "unavailable"` and
`evaluate(Telemetry) -> AnomalyVerdict | None`. It emits
`AnomalyMethod.ISOLATION_FOREST` with reason `multivariate_outlier` and the
normalised score. `bootstrap.py` constructs `UnavailableInference()` today and
would construct this instead; **no approved API, MQTT or database contract
changes.**

A missing, corrupt, mismatched or untrusted artifact leaves readiness at
`unavailable` and produces no verdict — ingestion and rules continue untouched.
Taking the pipeline down because a model is absent would be a worse failure
than having no model. The adapter keeps a per-device window and drops it on
reboot, a sequence gap, a time gap, or a rejected reading; until 30 new
contiguous samples arrive it returns `None` — silence, not a guess.

Compatibility tests import the backend's real `Telemetry`, `AnomalyVerdict` and
`InferenceEngine` from `backend/src`, so a contract change breaks them here
rather than in production.

## Reading a report honestly

`normal_flag_rate` counts windows whose *anchor* row carries no injected label.
A window spans 30 samples, so a window anchored shortly after an injected
scenario still contains disturbed samples and may legitimately flag. The
reported rate is therefore not a false-positive rate, on synthetic data or
otherwise — it is a count, and computing a false-positive rate needs
operator-approved normal data that does not yet exist.

## NOT_RUN

- Training or evaluation on real device data.
- Any measurement of real-world recall or false-positive rate.
- Model promotion into a running backend.
- Hardware-in-the-loop verification.
