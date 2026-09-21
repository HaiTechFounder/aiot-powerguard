"""The backend's `InferenceEngine`, backed by a trained artifact.

This is the only module that touches the backend, and it touches it in one
direction: it implements `powerguard.domain.ports.InferenceEngine` and returns
`AnomalyVerdict`. No approved contract changes -- `bootstrap.py` constructs
`UnavailableInference()` today and would construct this instead.

Two rules decide whether a verdict is even possible:

  1. **An unusable artifact is not an error, it is `unavailable`.** A missing,
     corrupt, mismatched or untrusted artifact leaves readiness at
     `unavailable`; ingestion and rules continue exactly as before. Refusing to
     start because a model is absent would take the whole pipeline down with it.
  2. **A window is 30 contiguous samples or it does not exist.** The adapter
     keeps a per-device buffer and drops it on reboot, a sequence gap, or a
     time gap over twice the cadence. Until 30 new samples arrive, `evaluate`
     returns None -- silence, not a guess.

`evaluate` is called once per accepted reading from the MQTT ingestion path, so
it stays allocation-light and never blocks.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from powerguard_ml.artifact import ArtifactError
from powerguard_ml.artifact import load as load_artifact
from powerguard_ml.dataset import Sample
from powerguard_ml.features import WINDOW, window_vector
from powerguard_ml.model import TrainedModel
from powerguard_ml.preprocess import CADENCE_SECONDS, GAP_TOLERANCE_FACTOR

READY = "ready"
UNAVAILABLE = "unavailable"
#: `ML_ARCHITECTURE.md` fixes the reason code for a model-only verdict.
REASON_MULTIVARIATE_OUTLIER = "multivariate_outlier"


class TelemetryLike(Protocol):
    """What the adapter reads. `powerguard.domain.entities.Telemetry` satisfies it."""

    device_id: str
    boot_id: str
    seq: int
    received_at: dt.datetime
    voltage_v: float
    current_a: float
    power_w: float
    energy_wh: float
    sensor_status: str


def _verdict_types() -> tuple[Any, Any]:
    """The backend's own verdict types, imported only when it is importable.

    The prototype is testable without the backend installed; the adapter is
    not useful without it. Importing lazily keeps both true.
    """
    from powerguard.domain.entities import AnomalyMethod, AnomalyVerdict

    return AnomalyMethod, AnomalyVerdict


class IsolationForestInference:
    """`InferenceEngine` over a locally trained, checksum-verified artifact."""

    def __init__(
        self,
        artifact_directory: Path | str | None,
        *,
        device_id: str | None = None,
        calibration_fingerprint: str | None = None,
        cadence_seconds: float = CADENCE_SECONDS,
        model_version: str | None = None,
    ) -> None:
        self._cadence = cadence_seconds
        self._buffers: dict[str, deque[TelemetryLike]] = {}
        self._runs: dict[str, int] = {}
        self._model: TrainedModel | None = None
        self._metadata: dict[str, Any] = {}
        self._model_version = model_version
        #: The only device this artifact may be asked about.
        self._artifact_device_id: str | None = None
        #: Why the model is unavailable, for logs and the health endpoint.
        self.unavailable_reason: str | None = None

        if artifact_directory is None:
            self.unavailable_reason = "no artifact configured"
            return
        try:
            model, metadata = load_artifact(
                artifact_directory,
                device_id=device_id,
                calibration_fingerprint=calibration_fingerprint,
            )
        except (ArtifactError, OSError) as failure:
            # An unusable artifact must never take ingestion down with it.
            # `artifact.load` turns every malformed-metadata and failed-
            # deserialisation path into an ArtifactError, so this is the whole
            # surface; anything else here would be a bug worth seeing.
            self.unavailable_reason = str(failure)
            return

        owner = metadata.get("device_id")
        if not isinstance(owner, str) or not owner:
            self.unavailable_reason = "artifact metadata names no device"
            return

        self._model = model
        self._metadata = metadata
        self._artifact_device_id = owner
        self._model_version = model_version or str(metadata.get("model_version", ""))

    # -- InferenceEngine ---------------------------------------------------

    def readiness(self) -> str:
        return READY if self._model is not None else UNAVAILABLE

    def evaluate(self, telemetry: TelemetryLike) -> Any:
        """A positive verdict, or None. Never raises into the ingestion path."""
        if self._model is None:
            return None
        if telemetry.device_id != self._artifact_device_id:
            # A model is fitted to one device's electrical behaviour. Scoring
            # another device against it would be a verdict about a load this
            # artifact has never seen, so the reading is not scored and not
            # buffered -- it leaves no trace in the owning device's window.
            return None
        if telemetry.sensor_status != "ok":
            # A rejected reading is not evidence about the load.
            self._reset(telemetry.device_id)
            return None

        buffer = self._buffer_for(telemetry)
        buffer.append(telemetry)
        if len(buffer) < WINDOW:
            return None

        vector = window_vector([_as_sample(row) for row in buffer])
        score = float(self._model.score(vector.reshape(1, -1))[0])

        device = telemetry.device_id
        if score < self._model.threshold:
            self._runs[device] = 0
            return None

        run = self._runs.get(device, 0) + 1
        self._runs[device] = run
        if run < self._model.consecutive:
            # One extreme window at a 2-second cadence is noise, not a verdict.
            return None

        method, verdict = _verdict_types()
        return verdict(
            method=method.ISOLATION_FOREST,
            reasons=(REASON_MULTIVARIATE_OUTLIER,),
            score=score,
            model_version=self._model_version or None,
        )

    # -- window bookkeeping ------------------------------------------------

    def _buffer_for(self, telemetry: TelemetryLike) -> deque[TelemetryLike]:
        device = telemetry.device_id
        buffer = self._buffers.get(device)
        if buffer is None:
            buffer = deque(maxlen=WINDOW)
            self._buffers[device] = buffer
            return buffer
        if buffer and not _contiguous(buffer[-1], telemetry, self._cadence):
            # A reboot or a hole invalidates the window; 30 new samples are
            # needed before the next vector means anything.
            buffer.clear()
            self._runs[device] = 0
        return buffer

    def _reset(self, device_id: str) -> None:
        buffer = self._buffers.get(device_id)
        if buffer is not None:
            buffer.clear()
        self._runs[device_id] = 0

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)


def _contiguous(previous: TelemetryLike, current: TelemetryLike, cadence: float) -> bool:
    if current.boot_id != previous.boot_id or current.seq != previous.seq + 1:
        return False
    elapsed = (current.received_at - previous.received_at).total_seconds()
    return 0 < elapsed <= cadence * GAP_TOLERANCE_FACTOR


def _as_sample(row: TelemetryLike) -> Sample:
    """Adapt a backend `Telemetry` to the `Sample` the feature code reads."""
    return Sample(
        id=getattr(row, "id", None) or 0,
        device_id=row.device_id,
        boot_id=row.boot_id,
        seq=row.seq,
        received_at=row.received_at,
        voltage_v=row.voltage_v,
        current_a=row.current_a,
        power_w=row.power_w,
        energy_wh=row.energy_wh,
        sensor_status=row.sensor_status,
    )


def score_windows(model: TrainedModel, matrix: np.ndarray) -> np.ndarray:
    """Normalised scores for a prepared matrix; used by the evaluation CLI."""
    return model.score(matrix)
