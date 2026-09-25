"""Saving a model, and refusing to load one that cannot be trusted.

``joblib.load`` executes what it unpickles, so an artifact is not a document --
it is code. Nothing here downloads one, and nothing loads one whose checksum
disagrees with its metadata. The metadata is checked *before* the pickle is
opened, because a checksum verified afterwards has already lost.

Beyond integrity, three things must match or the model is answering about a
different world: the device, the feature version, and the calibration regime.

A fourth decides whether the model can answer at all: `expected_seq_stride`,
the `seq` advance the training data was cut with. The online adapter must cut
windows by the same rule, so the stride travels in the metadata and is never
guessed at load time -- an artifact that does not declare one is refused.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pickle
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn

from powerguard_ml import __version__ as ml_version
from powerguard_ml.features import FEATURE_NAMES, FEATURE_VERSION, WINDOW
from powerguard_ml.model import MODEL_KIND, TrainedModel
from powerguard_ml.preprocess import check_stride

METADATA_VERSION = "powerguard_artifact_v1"
MODEL_FILE = "model.joblib"
METADATA_FILE = "metadata.json"

#: Fixed by the hardware design; a change is a new calibration regime.
SHUNT_RESISTANCE_OHM = 0.01


class ArtifactError(ValueError):
    """An artifact is absent, malformed, or not the one that was expected."""


@dataclass(frozen=True, slots=True)
class Metadata:
    model_version: str
    device_id: str
    created_at: str
    #: The `seq` advance the training data was cut with. No default on
    #: purpose: whoever writes an artifact states it, so it is never inferred.
    expected_seq_stride: int
    metadata_version: str = METADATA_VERSION
    feature_version: str = FEATURE_VERSION
    feature_names: tuple[str, ...] = FEATURE_NAMES
    window: int = WINDOW
    cadence_seconds: float = 2.0
    model_kind: str = MODEL_KIND
    threshold: float = 0.99
    consecutive: int = 2
    random_state: int = 42
    training_samples: int = 0
    training_windows: int = 0
    calibration_windows: int = 0
    training_interval: tuple[str | None, str | None] = (None, None)
    calibration_scores: tuple[float, ...] = ()
    shunt_resistance_ohm: float = SHUNT_RESISTANCE_OHM
    max_expected_current_a: float | None = None
    calibration_fingerprint: str | None = None
    libraries: dict[str, str] = field(default_factory=dict)
    #: Engineering metrics only. Never a claim about real-world accuracy.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: ``quality_not_established`` until enough operator-approved real data exists.
    data_quality: str = "quality_not_established"
    data_provenance: str = "synthetic"
    model_sha256: str = ""


def library_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit-learn": sklearn.__version__,
        "joblib": joblib.__version__,
        "powerguard_ml": ml_version,
    }


def _as_int(value: object, name: str) -> int:
    """Coerce a metadata field, reporting a bad one as an `ArtifactError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ArtifactError(f"metadata {name} is not an integer: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as failure:
        raise ArtifactError(f"metadata {name} is not an integer: {value!r}") from failure


def _as_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ArtifactError(f"metadata {name} is not a number: {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as failure:
        raise ArtifactError(f"metadata {name} is not a number: {value!r}") from failure


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_dir(root: Path | str, device_id: str, model_version: str) -> Path:
    return Path(root) / device_id / model_version


def save(model: TrainedModel, metadata: Metadata, directory: Path | str) -> Path:
    """Write ``model.joblib`` then ``metadata.json`` carrying its checksum."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    model_path = target / MODEL_FILE
    joblib.dump(model.pipeline, model_path)

    payload = asdict(metadata)
    payload["feature_names"] = list(metadata.feature_names)
    payload["training_interval"] = list(metadata.training_interval)
    payload["calibration_scores"] = [float(value) for value in model.calibration_scores]
    payload["threshold"] = float(model.threshold)
    payload["consecutive"] = int(model.consecutive)
    payload["libraries"] = metadata.libraries or library_versions()
    payload["model_sha256"] = _sha256(model_path)
    (target / METADATA_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return target


def read_metadata(directory: Path | str) -> dict[str, Any]:
    path = Path(directory) / METADATA_FILE
    if not path.exists():
        raise ArtifactError(f"no {METADATA_FILE} in {directory}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as failure:
        # Not UTF-8, not JSON, or nested past the parser's limit: all three are
        # a malformed document, and all three must surface as a refusal.
        raise ArtifactError(
            f"{path}: metadata is not JSON: {type(failure).__name__}: {failure}"
        ) from failure
    if not isinstance(payload, dict):
        raise ArtifactError(f"{path}: metadata is not an object")
    return payload


def stride_of(payload: dict[str, Any]) -> int:
    """The artifact's declared `seq` stride, strictly validated."""
    try:
        return check_stride(payload.get("expected_seq_stride"))
    except ValueError as failure:
        raise ArtifactError(f"metadata {failure}") from failure


def validate(
    payload: dict[str, Any],
    directory: Path | str,
    *,
    device_id: str | None = None,
    calibration_fingerprint: str | None = None,
) -> None:
    """Every reason to refuse, checked before anything is unpickled."""
    target = Path(directory)
    required = (
        "metadata_version",
        "model_version",
        "device_id",
        "feature_version",
        "feature_names",
        "window",
        "expected_seq_stride",
        "threshold",
        "calibration_scores",
        "model_sha256",
    )
    missing = [name for name in required if name not in payload]
    if missing == ["expected_seq_stride"]:
        # An artifact written before the stride was recorded. Its training
        # windows were cut with *some* rule, and guessing which one would let
        # the adapter score windows the model never saw. Retrain it instead.
        raise ArtifactError(
            "metadata does not declare expected_seq_stride (a legacy artifact); the "
            "window rule it was trained with is unknown, so it is refused. Retrain it."
        )
    if missing:
        raise ArtifactError(f"metadata is missing: {', '.join(missing)}")

    if payload["metadata_version"] != METADATA_VERSION:
        raise ArtifactError(
            f"metadata version {payload['metadata_version']!r} is not {METADATA_VERSION!r}"
        )
    if payload["feature_version"] != FEATURE_VERSION:
        raise ArtifactError(
            f"artifact was built for {payload['feature_version']!r}, "
            f"this build computes {FEATURE_VERSION!r}"
        )
    # Metadata is untrusted input, so every coercion below is a validation step
    # that must fail as an ArtifactError rather than as a raw TypeError.
    names = payload["feature_names"]
    if not isinstance(names, list) or tuple(names) != FEATURE_NAMES:
        raise ArtifactError("feature order differs from this build's feature contract")
    if _as_int(payload["window"], "window") != WINDOW:
        raise ArtifactError(f"artifact window {payload['window']!r} is not {WINDOW}")
    stride_of(payload)
    _as_float(payload["threshold"], "threshold")
    if "consecutive" in payload:
        _as_int(payload["consecutive"], "consecutive")
    if device_id is not None and payload["device_id"] != device_id:
        raise ArtifactError(
            f"artifact belongs to device {payload['device_id']!r}, not {device_id!r}"
        )
    if (
        calibration_fingerprint is not None
        and payload.get("calibration_fingerprint") != calibration_fingerprint
    ):
        raise ArtifactError(
            "artifact was trained under a different calibration regime; "
            "a new regime requires a new baseline and model"
        )
    scores = payload["calibration_scores"]
    if not isinstance(scores, list):
        raise ArtifactError(
            f"calibration_scores must be a list of numbers, not {type(scores).__name__}"
        )
    if not scores:
        raise ArtifactError("artifact carries no calibration distribution to score against")
    for value in scores:
        _as_float(value, "calibration_scores entry")

    model_path = target / MODEL_FILE
    if not model_path.exists():
        raise ArtifactError(f"no {MODEL_FILE} in {target}")
    actual = _sha256(model_path)
    if actual != payload["model_sha256"]:
        raise ArtifactError(
            f"{MODEL_FILE} checksum {actual[:12]} does not match metadata "
            f"{str(payload['model_sha256'])[:12]}; refusing to load"
        )


def load(
    directory: Path | str,
    *,
    device_id: str | None = None,
    calibration_fingerprint: str | None = None,
) -> tuple[TrainedModel, dict[str, Any]]:
    """Validate, then load. An invalid artifact raises and is never unpickled."""
    payload = read_metadata(directory)
    validate(
        payload,
        directory,
        device_id=device_id,
        calibration_fingerprint=calibration_fingerprint,
    )
    # `validate` proved this is a non-empty list of numbers.
    scores = payload["calibration_scores"]
    # Deserialisation is the boundary where a bad file becomes a Python error.
    # Every failure mode here is *data*, not a bug in this module, so each is
    # reported as a refusal. Anything outside this list is a programmer error
    # and is deliberately left to propagate.
    try:
        pipeline = joblib.load(Path(directory) / MODEL_FILE)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        IndexError,
        EOFError,
        ImportError,
        RecursionError,
        NotImplementedError,
        MemoryError,
        pickle.UnpicklingError,
        OSError,
    ) as failure:
        raise ArtifactError(
            f"{MODEL_FILE} could not be deserialised: {type(failure).__name__}: {failure}"
        ) from failure

    if not hasattr(pipeline, "score_samples"):
        raise ArtifactError(
            f"{MODEL_FILE} does not hold a scoring pipeline "
            f"(loaded a {type(pipeline).__name__})"
        )

    model = TrainedModel(
        pipeline=pipeline,
        calibration_scores=np.sort(
            np.asarray(
                [_as_float(value, "calibration_scores entry") for value in scores],
                dtype=np.float64,
            )
        ),
        threshold=_as_float(payload["threshold"], "threshold"),
        consecutive=_as_int(payload.get("consecutive", 2), "consecutive"),
    )
    return model, payload


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
