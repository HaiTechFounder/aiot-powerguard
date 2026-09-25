"""Deciding whether an anomaly model may be loaded at all.

Every path through this module ends in a working `InferenceEngine`. A refusal
returns :class:`UnavailableInference` and a reason -- it never raises into
startup, because taking ingestion, MQTT, history and the dashboard down over an
absent model would be a far worse failure than having no model.

The checks are layered, and the cheap ones come first so that a rejected
artifact is never unpickled:

1. **Opt-in.** Disabled by default. Nothing is loaded unless configuration
   explicitly names an artifact, a device and a calibration regime; enabling
   it with any of the three missing is refused here, not in `Settings`, so a
   half-written `.env` costs the model and nothing else.
2. **Dependency.** `powerguard_ml` is an optional dependency of the backend;
   its absence is a configuration problem with a one-line fix, not a crash.
3. **Provenance.** Unless explicitly relaxed, metadata must say
   `validated_real_data`. A synthetic-trained artifact is refused here by name,
   so it cannot be promoted by accident.
4. **Identity and integrity.** Device, calibration regime, feature version,
   window, the declared `expected_seq_stride` and the SHA-256 of
   `model.joblib` are all checked by `powerguard_ml.artifact.load` *before*
   the pickle is opened. An artifact that predates the stride is refused, not
   given a guessed one.
5. **Mode.** Even a fully trusted artifact starts in shadow unless
   configuration says `active`.

Anything the optional ML package raises beyond its documented refusals is
caught at the outer boundary and reported the same way: `build_inference` is
called once, during startup, and nothing it touches may stop startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powerguard.config import Settings
from powerguard.domain.ports import InferenceEngine
from powerguard.inference.shadow import ShadowCounters, ShadowInference
from powerguard.inference.unavailable import UnavailableInference
from powerguard.observability import log_event

logger = logging.getLogger(__name__)

#: The only `data_quality` an artifact may carry and still be activated.
VALIDATED_QUALITY = "validated_real_data"


@dataclass(frozen=True, slots=True)
class InferenceLoad:
    """What was wired, and why it was or was not the real thing."""

    engine: InferenceEngine
    #: `active`, `shadow`, or `unavailable`.
    mode: str
    reason: str
    model_version: str | None = None
    data_quality: str | None = None
    #: The `seq` advance live windows are cut with, from the artifact.
    expected_seq_stride: int | None = None
    shadow_counters: ShadowCounters | None = None

    @property
    def loaded(self) -> bool:
        return self.mode in ("active", "shadow")


def _unavailable(
    reason: str, *, level: int = logging.WARNING, **fields: object
) -> InferenceLoad:
    # WARNING by default: every refusal below happens because somebody asked
    # for inference and is not getting it, which an operator needs to see.
    log_event(logger, level, "inference.unavailable", reason=reason, **fields)
    return InferenceLoad(engine=UnavailableInference(), mode="unavailable", reason=reason)


def build_inference(settings: Settings) -> InferenceLoad:
    """Wire the anomaly engine for this process. Never raises."""
    if not settings.inference_enabled:
        return _unavailable("inference is disabled by configuration", level=logging.INFO)
    try:
        return _build_enabled(settings)
    except Exception as failure:
        # The documented refusals are handled inside. This is the net under
        # them: an optional dependency must not be able to stop startup with
        # an exception type nobody anticipated.
        logger.exception("inference loader failed unexpectedly; continuing without a model")
        return _unavailable(
            f"artifact could not be loaded: {type(failure).__name__}: {failure}",
            device_id=settings.inference_device_id,
        )


def _build_enabled(settings: Settings) -> InferenceLoad:
    directory = settings.inference_artifact_dir
    device_id = settings.inference_device_id
    fingerprint = settings.inference_calibration_fingerprint
    if directory is None or not device_id or not fingerprint:
        # A blank calibration fingerprint in particular must never mean "any".
        missing = [
            name
            for name, value in (
                ("POWERGUARD_INFERENCE_ARTIFACT_DIR", directory),
                ("POWERGUARD_INFERENCE_DEVICE_ID", device_id),
                ("POWERGUARD_INFERENCE_CALIBRATION_FINGERPRINT", fingerprint),
            )
            if not value
        ]
        return _unavailable(
            "inference is enabled but not fully configured; missing " + ", ".join(missing)
        )

    target = Path(directory)
    if not target.is_dir():
        return _unavailable(f"no artifact directory at {target}", device_id=device_id)

    try:
        from powerguard_ml.adapter import IsolationForestInference
        from powerguard_ml.artifact import ArtifactError, read_metadata, validate
    except ImportError as failure:
        return _unavailable(
            "powerguard_ml is not installed in this environment "
            f"({failure}); install it into the backend virtualenv to enable inference",
            device_id=device_id,
        )

    # Metadata first: provenance and identity are decided before any pickle is
    # opened, because a checksum verified after loading has already lost.
    try:
        metadata: dict[str, Any] = read_metadata(target)
        validate(
            metadata,
            target,
            device_id=device_id,
            calibration_fingerprint=fingerprint,
        )
    except (ArtifactError, OSError) as failure:
        return _unavailable(f"artifact rejected: {failure}", device_id=device_id)

    quality = str(metadata.get("data_quality", "")) or None
    provenance = str(metadata.get("data_provenance", "")) or None
    if settings.inference_require_validated and quality != VALIDATED_QUALITY:
        return _unavailable(
            f"artifact data_quality is {quality!r}, not {VALIDATED_QUALITY!r}; "
            "it has not passed the real-data gate and will not be loaded",
            device_id=device_id,
            data_provenance=provenance,
        )
    if not settings.inference_require_validated:
        log_event(
            logger,
            logging.WARNING,
            "inference.validation_requirement_disabled",
            device_id=device_id,
            data_quality=quality,
            data_provenance=provenance,
            note="development affordance: an unvalidated artifact is being loaded",
        )

    engine = IsolationForestInference(
        target,
        device_id=device_id,
        calibration_fingerprint=fingerprint,
    )
    if engine.readiness() != "ready":
        return _unavailable(
            f"artifact could not be loaded: {engine.unavailable_reason}",
            device_id=device_id,
        )

    model_version = str(metadata.get("model_version", "")) or None
    stride = getattr(engine, "expected_seq_stride", None)
    validated = quality == VALIDATED_QUALITY

    # Relaxing INFERENCE_REQUIRE_VALIDATED lets an unvalidated artifact be
    # *observed*; it never lets one act. Activation always requires
    # `validated_real_data`, whatever else configuration says.
    if settings.inference_mode == "active" and not validated:
        log_event(
            logger,
            logging.WARNING,
            "inference.activation_refused",
            device_id=device_id,
            data_quality=quality,
            data_provenance=provenance,
            note="unvalidated artifact: running in shadow instead of active",
        )

    if settings.inference_mode == "active" and validated:
        log_event(
            logger,
            logging.INFO,
            "inference.active",
            device_id=device_id,
            model_version=model_version,
            data_quality=quality,
            calibration_fingerprint=fingerprint,
            expected_seq_stride=stride,
        )
        return InferenceLoad(
            engine=engine,
            mode="active",
            reason="artifact validated and activated",
            model_version=model_version,
            data_quality=quality,
            expected_seq_stride=stride,
        )

    counters = ShadowCounters()
    log_event(
        logger,
        logging.INFO,
        "inference.shadow",
        device_id=device_id,
        model_version=model_version,
        data_quality=quality,
        expected_seq_stride=stride,
        note="scoring live telemetry; verdicts are logged and discarded",
    )
    return InferenceLoad(
        engine=ShadowInference(engine, counters=counters),
        mode="shadow",
        reason=(
            "artifact validated; running in shadow, no verdict is emitted"
            if validated
            else "artifact is not validated real data; shadow only, activation refused"
        ),
        model_version=model_version,
        data_quality=quality,
        expected_seq_stride=stride,
        shadow_counters=counters,
    )
