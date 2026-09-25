"""What the backend will and will not load, and what happens when it refuses.

Every case here is a way a model could end up scoring something it was never
fitted on, or a way an unvalidated artifact could be mistaken for a promoted
one. The rule under test throughout: a refusal is a logged `unavailable`, and
ingestion, MQTT, history and the API carry on regardless.

`powerguard_ml` is an optional dependency. The tests that need a real artifact
skip when it is absent rather than pretending to have exercised it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from powerguard.config import Settings
from powerguard.inference.loader import VALIDATED_QUALITY, build_inference
from powerguard.inference.shadow import ShadowCounters, ShadowInference
from powerguard.inference.unavailable import UnavailableInference

ml = pytest.importorskip("powerguard_ml", reason="powerguard_ml is an optional dependency")

from powerguard_ml import artifact as ml_artifact  # noqa: E402
from powerguard_ml import synthetic  # noqa: E402
from powerguard_ml.features import build_matrix  # noqa: E402
from powerguard_ml.model import train as train_model  # noqa: E402
from powerguard_ml.preprocess import prepare, time_split  # noqa: E402

DEVICE = "powerguard-01"
REGIME = "ina226-r010-bench-a"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"mqtt_enabled": False}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="module")
def artifact_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real, checksum-valid artifact, built from synthetic rows.

    It is deliberately *not* validated data: `data_quality` stays
    `quality_not_established`, which is exactly what the default loader must
    refuse. Tests that need a loadable artifact rewrite that one field.
    """
    rows = synthetic.training_set(900, seed=7)
    prepared = prepare(rows)
    usable = [row for segment in prepared.segments for row in segment]
    split = time_split(usable, train_fraction=0.8)
    train_matrix, _ = build_matrix(prepare(split.train).segments)
    calibration_matrix, _ = build_matrix(prepare(split.calibration).segments)
    model = train_model(train_matrix, calibration_matrix)

    directory = tmp_path_factory.mktemp("artifacts") / DEVICE / "v1"
    metadata = ml_artifact.Metadata(
        model_version="v1",
        device_id=DEVICE,
        created_at=ml_artifact.now_utc(),
        expected_seq_stride=2,
        calibration_fingerprint=REGIME,
        libraries=ml_artifact.library_versions(),
        data_quality="quality_not_established",
        data_provenance="synthetic",
    )
    ml_artifact.save(model, metadata, directory)
    return directory


def _rewrite_metadata(directory: Path, **changes: Any) -> None:
    path = directory / ml_artifact.METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _validated(directory: Path) -> None:
    _rewrite_metadata(directory, data_quality=VALIDATED_QUALITY, data_provenance="hardware")


# -- the switch -------------------------------------------------------------


def test_inference_is_off_unless_asked_for() -> None:
    load = build_inference(_settings())
    assert load.mode == "unavailable"
    assert isinstance(load.engine, UnavailableInference)
    assert load.engine.readiness() == "unavailable"


def test_enabling_without_naming_an_artifact_degrades_rather_than_stopping_startup() -> None:
    # `Settings` accepts it -- raising there would take ingestion, REST and the
    # WebSocket down over an optional model -- and the loader refuses it.
    load = build_inference(_settings(inference_enabled=True))
    assert load.mode == "unavailable"
    assert isinstance(load.engine, UnavailableInference)
    assert "POWERGUARD_INFERENCE_ARTIFACT_DIR" in load.reason
    assert "POWERGUARD_INFERENCE_CALIBRATION_FINGERPRINT" in load.reason


def test_a_blank_calibration_regime_never_means_any(tmp_path: Path) -> None:
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=tmp_path,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint="",
        )
    )
    assert load.mode == "unavailable"
    assert load.reason.endswith("POWERGUARD_INFERENCE_CALIBRATION_FINGERPRINT")


# -- the real-data gate, enforced at load time ------------------------------


def test_an_unvalidated_artifact_is_refused(artifact_root: Path) -> None:
    _rewrite_metadata(
        artifact_root, data_quality="quality_not_established", data_provenance="synthetic"
    )
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=artifact_root,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint=REGIME,
        )
    )
    assert load.mode == "unavailable"
    assert "data_quality" in load.reason
    assert load.engine.readiness() == "unavailable"


def test_a_validated_artifact_loads_into_shadow_by_default(artifact_root: Path) -> None:
    _validated(artifact_root)
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=artifact_root,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint=REGIME,
        )
    )
    assert load.mode == "shadow"
    assert isinstance(load.engine, ShadowInference)
    # Shadow never claims readiness: nothing it says reaches the system.
    assert load.engine.readiness() == "unavailable"
    # Live windows are cut with the stride the artifact was trained with.
    assert load.expected_seq_stride == 2


def test_activation_is_explicit(artifact_root: Path) -> None:
    _validated(artifact_root)
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=artifact_root,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint=REGIME,
            inference_mode="active",
        )
    )
    assert load.mode == "active"
    assert load.engine.readiness() == "ready"


# -- identity and integrity -------------------------------------------------


def test_a_foreign_device_is_refused(artifact_root: Path) -> None:
    _validated(artifact_root)
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=artifact_root,
            inference_device_id="someone-elses-device",
            inference_calibration_fingerprint=REGIME,
            inference_mode="active",
        )
    )
    assert load.mode == "unavailable"
    assert "device" in load.reason


def test_a_different_calibration_regime_is_refused(artifact_root: Path) -> None:
    _validated(artifact_root)
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=artifact_root,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint="a-different-shunt",
            inference_mode="active",
        )
    )
    assert load.mode == "unavailable"
    assert "calibration" in load.reason


def test_a_tampered_model_file_is_refused(artifact_root: Path, tmp_path: Path) -> None:
    _validated(artifact_root)
    copy = tmp_path / DEVICE / "v1"
    copy.mkdir(parents=True)
    for name in (ml_artifact.MODEL_FILE, ml_artifact.METADATA_FILE):
        (copy / name).write_bytes((artifact_root / name).read_bytes())
    # One byte is enough: the checksum is checked before the pickle is opened.
    (copy / ml_artifact.MODEL_FILE).write_bytes(
        (copy / ml_artifact.MODEL_FILE).read_bytes() + b"\x00"
    )

    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=copy,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint=REGIME,
            inference_mode="active",
        )
    )
    assert load.mode == "unavailable"
    assert "checksum" in load.reason


def test_a_missing_artifact_directory_is_refused(tmp_path: Path) -> None:
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=tmp_path / "does-not-exist",
            inference_device_id=DEVICE,
            inference_calibration_fingerprint=REGIME,
        )
    )
    assert load.mode == "unavailable"
    assert "artifact directory" in load.reason


def test_an_empty_directory_is_refused_not_crashed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    load = build_inference(
        _settings(
            inference_enabled=True,
            inference_artifact_dir=empty,
            inference_device_id=DEVICE,
            inference_calibration_fingerprint=REGIME,
        )
    )
    assert load.mode == "unavailable"
    assert load.engine.readiness() == "unavailable"


# -- shadow behaviour -------------------------------------------------------


class _AlwaysFlags:
    def readiness(self) -> str:
        return "ready"

    def evaluate(self, telemetry: object) -> object:
        del telemetry
        return _Verdict()


class _Raises:
    def readiness(self) -> str:
        return "ready"

    def evaluate(self, telemetry: object) -> object:
        del telemetry
        raise RuntimeError("model blew up")


class _Verdict:
    score = 0.99
    method = "isolation_forest"


class _Telemetry:
    device_id = DEVICE
    seq = 1


def test_shadow_counts_but_never_emits() -> None:
    counters = ShadowCounters()
    engine = ShadowInference(_AlwaysFlags(), counters=counters)  # type: ignore[arg-type]

    assert engine.evaluate(_Telemetry()) is None  # type: ignore[arg-type]
    assert engine.evaluate(_Telemetry()) is None  # type: ignore[arg-type]

    assert counters.evaluated == 2
    # It would have flagged twice. No anomaly row, no WS frame, no alert.
    assert counters.would_flag == 2
    assert engine.readiness() == "unavailable"


def test_a_shadow_model_that_raises_does_not_reach_ingestion() -> None:
    counters = ShadowCounters()
    engine = ShadowInference(_Raises(), counters=counters)  # type: ignore[arg-type]

    assert engine.evaluate(_Telemetry()) is None  # type: ignore[arg-type]

    assert counters.errors == 1
    assert counters.would_flag == 0


# -- malformed metadata and unexpected failures degrade, never raise --------


def _copy_artifact(source: Path, target: Path) -> Path:
    target.mkdir(parents=True)
    for name in (ml_artifact.MODEL_FILE, ml_artifact.METADATA_FILE):
        (target / name).write_bytes((source / name).read_bytes())
    return target


def _enabled(directory: Path, **overrides: Any) -> Settings:
    return _settings(
        inference_enabled=True,
        inference_artifact_dir=directory,
        inference_device_id=DEVICE,
        inference_calibration_fingerprint=REGIME,
        inference_mode="active",
        **overrides,
    )


def test_a_legacy_artifact_without_a_stride_is_refused(
    artifact_root: Path, tmp_path: Path
) -> None:
    _validated(artifact_root)
    copy = _copy_artifact(artifact_root, tmp_path / "legacy")
    path = copy / ml_artifact.METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["expected_seq_stride"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    load = build_inference(_enabled(copy))
    assert load.mode == "unavailable"
    assert "legacy artifact" in load.reason


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe not utf-8", b"{ truncated", b"[1, 2, 3]", b"[" * 100_000 + b"]" * 100_000],
    ids=["not-utf8", "truncated", "not-an-object", "nested-past-the-parser"],
)
def test_malformed_metadata_documents_degrade_to_unavailable(
    tmp_path: Path, content: bytes
) -> None:
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / ml_artifact.METADATA_FILE).write_bytes(content)
    load = build_inference(_enabled(directory))
    assert load.mode == "unavailable"
    assert isinstance(load.engine, UnavailableInference)
    assert "artifact rejected" in load.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window", "thirty"),
        ("threshold", None),
        ("calibration_scores", "0.1,0.2"),
        ("expected_seq_stride", 0),
        ("expected_seq_stride", "2"),
        ("feature_names", 42),
    ],
)
def test_malformed_metadata_fields_degrade_to_unavailable(
    artifact_root: Path, tmp_path: Path, field: str, value: object
) -> None:
    _validated(artifact_root)
    copy = _copy_artifact(artifact_root, tmp_path / "malformed")
    _rewrite_metadata(copy, **{field: value})
    load = build_inference(_enabled(copy))
    assert load.mode == "unavailable"


def test_an_undeserialisable_model_degrades_to_unavailable(
    artifact_root: Path, tmp_path: Path
) -> None:
    _validated(artifact_root)
    copy = _copy_artifact(artifact_root, tmp_path / "garbage")
    (copy / ml_artifact.MODEL_FILE).write_bytes(b"\x80\x04\x95garbage-not-a-pickle")
    _rewrite_metadata(copy, model_sha256=ml_artifact._sha256(copy / ml_artifact.MODEL_FILE))
    load = build_inference(_enabled(copy))
    assert load.mode == "unavailable"
    assert "deserialised" in load.reason


def test_an_unexpected_exception_from_the_ml_package_cannot_stop_startup(
    artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _validated(artifact_root)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("an exception type nobody anticipated")

    monkeypatch.setattr(ml_artifact, "read_metadata", explode)
    load = build_inference(_enabled(artifact_root))
    assert load.mode == "unavailable"
    assert "RuntimeError" in load.reason
    assert load.engine.readiness() == "unavailable"


# -- relaxing validation is a development affordance, never an activation ----


def test_an_unvalidated_artifact_is_never_activated_even_with_validation_relaxed(
    artifact_root: Path, tmp_path: Path
) -> None:
    copy = _copy_artifact(artifact_root, tmp_path / "unvalidated")
    _rewrite_metadata(
        copy, data_quality="quality_not_established", data_provenance="synthetic"
    )
    load = build_inference(
        _enabled(copy, inference_require_validated=False)  # asks for mode="active"
    )
    # It may be observed in shadow, but its verdicts must never reach the system.
    assert load.mode == "shadow"
    assert isinstance(load.engine, ShadowInference)
    assert load.engine.readiness() == "unavailable"
    assert "not validated" in load.reason
