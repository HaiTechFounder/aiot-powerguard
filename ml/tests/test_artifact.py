"""Save, load, and every reason to refuse to load."""

from __future__ import annotations

import json

import numpy as np
import pytest

from powerguard_ml.artifact import (
    METADATA_FILE,
    METADATA_VERSION,
    MODEL_FILE,
    ArtifactError,
    Metadata,
    _sha256,
    load,
    now_utc,
    read_metadata,
    save,
)
from powerguard_ml.features import FEATURE_NAMES, build_matrix
from powerguard_ml.preprocess import prepare
from powerguard_ml.synthetic import CALIBRATION_FINGERPRINT, DEFAULT_DEVICE, generate


def _rewrite(directory, **changes) -> None:
    path = directory / METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_an_artifact_round_trips_with_identical_scores(trained_artifact) -> None:
    directory, original = trained_artifact
    reloaded, metadata = load(directory)

    matrix, _ = build_matrix(prepare(generate(80, seed=11, boot_id="boot-x")).segments)
    assert np.array_equal(original.score(matrix), reloaded.score(matrix))
    assert reloaded.threshold == original.threshold
    assert reloaded.consecutive == original.consecutive
    assert metadata["feature_names"] == list(FEATURE_NAMES)


def test_metadata_records_what_would_be_needed_to_reproduce_it(trained_artifact) -> None:
    directory, _ = trained_artifact
    payload = read_metadata(directory)
    for key in (
        "model_version",
        "device_id",
        "created_at",
        "feature_version",
        "window",
        "threshold",
        "calibration_scores",
        "libraries",
        "model_sha256",
        "shunt_resistance_ohm",
        "calibration_fingerprint",
    ):
        assert key in payload, key
    assert payload["libraries"]["scikit-learn"]
    assert payload["metadata_version"] == METADATA_VERSION


def test_a_prototype_never_claims_established_quality(trained_artifact) -> None:
    directory, _ = trained_artifact
    assert read_metadata(directory)["data_quality"] == "quality_not_established"


def test_a_tampered_model_is_refused_before_it_is_unpickled(trained_artifact) -> None:
    directory, _ = trained_artifact
    (directory / MODEL_FILE).write_bytes(b"not a model")
    with pytest.raises(ArtifactError, match="checksum"):
        load(directory)


def test_a_foreign_device_artifact_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    with pytest.raises(ArtifactError, match="belongs to device"):
        load(directory, device_id="some-other-device")


def test_a_different_calibration_regime_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    with pytest.raises(ArtifactError, match="calibration regime"):
        load(directory, calibration_fingerprint="rewired-shunt-v2")


def test_the_matching_device_and_regime_load(trained_artifact) -> None:
    directory, _ = trained_artifact
    model, _ = load(
        directory,
        device_id=DEFAULT_DEVICE,
        calibration_fingerprint=CALIBRATION_FINGERPRINT,
    )
    assert model.calibration_scores.size > 0


def test_a_stale_feature_version_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, feature_version="powerguard_features_v0")
    with pytest.raises(ArtifactError, match="was built for"):
        load(directory)


def test_reordered_features_are_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, feature_names=list(reversed(FEATURE_NAMES)))
    with pytest.raises(ArtifactError, match="feature order"):
        load(directory)


def test_a_different_window_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, window=10)
    with pytest.raises(ArtifactError, match="window"):
        load(directory)


def test_an_artifact_without_a_calibration_distribution_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, calibration_scores=[])
    with pytest.raises(ArtifactError, match="calibration distribution"):
        load(directory)


def test_an_unknown_metadata_version_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, metadata_version="powerguard_artifact_v99")
    with pytest.raises(ArtifactError, match="metadata version"):
        load(directory)


def test_missing_metadata_is_refused(tmp_path) -> None:
    with pytest.raises(ArtifactError, match=r"no metadata\.json"):
        load(tmp_path)


def test_unparseable_metadata_is_refused(tmp_path) -> None:
    (tmp_path / METADATA_FILE).write_text("{ not json", encoding="utf-8")
    with pytest.raises(ArtifactError, match="not JSON"):
        load(tmp_path)


def test_a_truncated_metadata_document_names_what_is_missing(tmp_path) -> None:
    (tmp_path / METADATA_FILE).write_text(json.dumps({"model_version": "v1"}), encoding="utf-8")
    with pytest.raises(ArtifactError, match="missing"):
        load(tmp_path)


def test_a_missing_model_file_is_refused(trained_artifact) -> None:
    directory, _ = trained_artifact
    (directory / MODEL_FILE).unlink()
    with pytest.raises(ArtifactError, match=r"no model\.joblib"):
        load(directory)


def test_saving_creates_the_directory(tmp_path, trained_artifact) -> None:
    _, model = trained_artifact
    target = tmp_path / "nested" / "device" / "v2"
    metadata = Metadata(
        model_version="v2", device_id="d", created_at=now_utc(), expected_seq_stride=2
    )
    save(model, metadata, target)
    assert (target / MODEL_FILE).exists()
    assert (target / METADATA_FILE).exists()


# -- malformed metadata is a refusal, never a raw Python error -------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window", "not-a-number"),
        ("window", None),
        ("window", {"nested": 1}),
        ("threshold", "high"),
        ("threshold", None),
        ("threshold", []),
        ("consecutive", "twice"),
        ("feature_names", "voltage_v"),
        ("feature_names", None),
        ("feature_names", 42),
        ("calibration_scores", "0.1,0.2"),
        ("calibration_scores", {"a": 1}),
        ("calibration_scores", [0.1, "x"]),
        ("calibration_scores", [0.1, None]),
        ("model_sha256", None),
    ],
)
def test_a_malformed_metadata_field_is_an_artifact_error(trained_artifact, field, value) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, **{field: value})
    # Not a TypeError, not a ValueError from deep inside numpy: a refusal the
    # adapter knows how to turn into `unavailable`.
    with pytest.raises(ArtifactError):
        load(directory)


def test_metadata_that_is_a_json_array_is_refused(tmp_path) -> None:
    (tmp_path / METADATA_FILE).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ArtifactError, match="not an object"):
        load(tmp_path)


def test_a_model_file_that_is_not_a_pipeline_is_refused(trained_artifact) -> None:
    import joblib

    directory, _ = trained_artifact
    joblib.dump({"not": "a pipeline"}, directory / MODEL_FILE)
    _rewrite(directory, model_sha256=_sha256(directory / MODEL_FILE))
    with pytest.raises(ArtifactError, match="scoring pipeline"):
        load(directory)


def test_an_undeserialisable_model_is_refused_not_propagated(trained_artifact) -> None:
    directory, _ = trained_artifact
    # Valid gzip-ish garbage: the checksum matches, so this gets past
    # validation and fails inside joblib, which is exactly the path under test.
    (directory / MODEL_FILE).write_bytes(b"\x80\x04\x95garbage-not-a-pickle")
    _rewrite(directory, model_sha256=_sha256(directory / MODEL_FILE))
    with pytest.raises(ArtifactError, match="could not be deserialised"):
        load(directory)


def test_a_boolean_is_not_accepted_as_a_number(trained_artifact) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, window=True)
    with pytest.raises(ArtifactError, match="not an integer"):
        load(directory)


# -- the seq stride travels with the artifact ------------------------------


def test_the_stride_is_recorded_in_the_metadata(trained_artifact) -> None:
    directory, _ = trained_artifact
    _, payload = load(directory)
    assert payload["expected_seq_stride"] == 2


def test_a_legacy_artifact_without_a_stride_is_refused_not_guessed(trained_artifact) -> None:
    directory, _ = trained_artifact
    path = directory / METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["expected_seq_stride"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="legacy artifact"):
        load(directory)


@pytest.mark.parametrize("value", [0, -1, 9, 1000, "2", 2.0, 2.5, True, None, [2]])
def test_an_invalid_stride_is_refused(trained_artifact, value) -> None:
    directory, _ = trained_artifact
    _rewrite(directory, expected_seq_stride=value)
    with pytest.raises(ArtifactError, match="expected_seq_stride"):
        load(directory)


def test_metadata_that_is_not_utf8_is_refused(tmp_path) -> None:
    (tmp_path / METADATA_FILE).write_bytes(b"\xff\xfe\x00{")
    with pytest.raises(ArtifactError, match="UnicodeDecodeError"):
        load(tmp_path)


def test_absurdly_nested_metadata_is_refused(tmp_path) -> None:
    (tmp_path / METADATA_FILE).write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(ArtifactError):
        load(tmp_path)
