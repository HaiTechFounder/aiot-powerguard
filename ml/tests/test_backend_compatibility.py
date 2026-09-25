"""The adapter, against the backend's real types -- not a stand-in for them.

`Telemetry`, `AnomalyVerdict`, `AnomalyMethod` and the `InferenceEngine`
protocol are imported from `backend/src` exactly as `bootstrap.py` would see
them. If the approved contract moves, these tests break here rather than in
production.
"""

from __future__ import annotations

import datetime as dt

from powerguard.domain.entities import AnomalyMethod, AnomalyVerdict, Telemetry
from powerguard.domain.ports import InferenceEngine
from powerguard.inference.unavailable import UnavailableInference

from powerguard_ml.adapter import (
    REASON_MULTIVARIATE_OUTLIER,
    UNAVAILABLE,
    IsolationForestInference,
)
from powerguard_ml.features import WINDOW
from powerguard_ml.synthetic import DEFAULT_DEVICE, Scenario, generate

EPOCH = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)


def as_telemetry(sample, **overrides) -> Telemetry:
    fields = {
        "device_id": sample.device_id,
        "boot_id": sample.boot_id,
        "seq": sample.seq,
        "received_at": sample.received_at,
        "voltage_v": sample.voltage_v,
        "current_a": sample.current_a,
        "power_w": sample.power_w,
        "energy_wh": sample.energy_wh,
        "sensor_status": sample.sensor_status,
        "id": sample.id,
    }
    return Telemetry(**(fields | overrides))


def feed(engine, samples, **overrides) -> list[AnomalyVerdict | None]:
    return [engine.evaluate(as_telemetry(sample, **overrides)) for sample in samples]


# -- the shape the backend expects ----------------------------------------


def accepts_engine(engine: InferenceEngine) -> InferenceEngine:
    """Typed at the backend's port, so mypy proves structural conformance.

    `InferenceEngine` is not `@runtime_checkable`, and making it so would mean
    editing an approved contract. The compiler checks the shape; this checks
    the signatures the shape is supposed to describe.
    """
    return engine


def test_the_adapter_satisfies_the_backend_protocol(trained_artifact) -> None:
    import inspect

    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    # The adapter it replaces is the reference for what the port looks like.
    for candidate in (accepts_engine(engine), accepts_engine(UnavailableInference())):
        assert callable(candidate.readiness)
        assert callable(candidate.evaluate)
        assert list(inspect.signature(candidate.evaluate).parameters) == ["telemetry"]
        assert inspect.signature(candidate.readiness).parameters == {}


def test_readiness_matches_the_vocabulary_the_health_endpoint_publishes(
    trained_artifact,
) -> None:
    directory, _ = trained_artifact
    assert IsolationForestInference(directory).readiness() == "ready"
    assert UnavailableInference().readiness() == UNAVAILABLE


# -- an unusable artifact degrades, it does not crash ----------------------


def test_no_artifact_configured_is_unavailable_not_an_error() -> None:
    engine = IsolationForestInference(None)
    assert engine.readiness() == UNAVAILABLE
    assert engine.unavailable_reason


def test_an_absent_artifact_directory_is_unavailable(tmp_path) -> None:
    engine = IsolationForestInference(tmp_path / "nothing-here")
    assert engine.readiness() == UNAVAILABLE


def test_a_corrupt_artifact_is_unavailable_rather_than_fatal(trained_artifact) -> None:
    directory, _ = trained_artifact
    (directory / "model.joblib").write_bytes(b"tampered")
    engine = IsolationForestInference(directory)
    assert engine.readiness() == UNAVAILABLE
    assert "checksum" in (engine.unavailable_reason or "")


def test_an_unavailable_engine_never_produces_a_verdict(tmp_path) -> None:
    engine = IsolationForestInference(tmp_path / "absent")
    assert feed(engine, generate(WINDOW + 10)) == [None] * (WINDOW + 10)


# -- window eligibility ----------------------------------------------------


def test_no_verdict_before_a_full_window_exists(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    assert feed(engine, generate(WINDOW - 1)) == [None] * (WINDOW - 1)


def test_a_reboot_invalidates_the_window(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    feed(engine, generate(WINDOW))
    # A new boot resets the sequence: 30 fresh samples are needed again.
    rebooted = generate(WINDOW - 1, boot_id="boot-new", start_id=9000, start_seq=1)
    assert feed(engine, rebooted) == [None] * (WINDOW - 1)


def test_a_sequence_hole_invalidates_the_window(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    rows = generate(WINDOW + 40, scenarios=(Scenario("spike", start=30, length=40),))
    holed = rows[:WINDOW] + rows[WINDOW + 5 :]
    verdicts = feed(engine, holed)
    # The first windows after the hole cannot be judged at all.
    assert verdicts[WINDOW : WINDOW + WINDOW - 1] == [None] * (WINDOW - 1)


def test_a_rejected_reading_drops_the_window(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    feed(engine, generate(WINDOW))
    assert engine.evaluate(as_telemetry(generate(1)[0], sensor_status="fault")) is None
    assert feed(engine, generate(WINDOW - 1, start_seq=200, start_id=7000)) == [None] * (
        WINDOW - 1
    )


def test_nominal_traffic_produces_no_verdict(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    verdicts = feed(engine, generate(200, seed=21, boot_id="boot-nominal"))
    assert all(verdict is None for verdict in verdicts)


# -- the verdict itself ----------------------------------------------------


def test_a_sustained_injected_departure_produces_a_contract_shaped_verdict(
    trained_artifact,
) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    rows = generate(
        220,
        seed=33,
        boot_id="boot-spike",
        start_id=20_000,
        scenarios=(Scenario("spike", start=120, length=60),),
    )
    verdicts = [verdict for verdict in feed(engine, rows) if verdict is not None]

    assert verdicts, "an injected spike should eventually be flagged on the fixture"
    verdict = verdicts[0]
    assert isinstance(verdict, AnomalyVerdict)
    assert verdict.method is AnomalyMethod.ISOLATION_FOREST
    assert verdict.reasons == (REASON_MULTIVARIATE_OUTLIER,)
    assert verdict.score is not None and 0.0 <= verdict.score <= 1.0
    assert verdict.model_version == "v1"


def test_a_single_extreme_window_is_suppressed(trained_artifact) -> None:
    directory, model = trained_artifact
    engine = IsolationForestInference(directory)
    assert model.consecutive == 2
    rows = generate(
        WINDOW + 1,
        seed=44,
        boot_id="boot-blip",
        start_id=30_000,
        scenarios=(Scenario("spike", start=WINDOW, length=1),),
    )
    # At most one window ends on the single disturbed sample, so suppression
    # leaves nothing to report.
    assert all(verdict is None for verdict in feed(engine, rows))


# -- one artifact, one device ---------------------------------------------


def test_another_device_is_never_scored_against_this_artifact(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    # Enough contiguous readings to build a full window several times over,
    # and a sustained departure that WOULD be flagged for the owning device.
    foreign = generate(
        220,
        seed=33,
        device_id="pg-not-this-one",
        boot_id="boot-foreign",
        start_id=50_000,
        scenarios=(Scenario("spike", start=120, length=60),),
    )
    assert all(verdict is None for verdict in feed(engine, foreign))


def test_a_foreign_reading_leaves_no_trace_in_the_owning_window(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    owned = generate(WINDOW, device_id=DEFAULT_DEVICE)

    # Interleave a foreign reading; it must neither be scored nor disturb the
    # owning device's window, which is complete by the last owned sample.
    for index, sample in enumerate(owned):
        engine.evaluate(as_telemetry(sample))
        if index == 10:
            stray = generate(1, device_id="pg-other", start_id=60_000)[0]
            assert engine.evaluate(as_telemetry(stray)) is None

    following = generate(
        1,
        device_id=DEFAULT_DEVICE,
        start_id=owned[-1].id + 1,
        start_seq=owned[-1].seq + 1,
        start_at=owned[-1].received_at + dt.timedelta(seconds=2),
    )[0]
    # A verdict is still possible for the owning device: None here means
    # "not anomalous", not "window destroyed by a foreign row".
    engine.evaluate(as_telemetry(following))
    assert engine.readiness() == "ready"


def test_a_device_mismatch_does_not_change_readiness(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    feed(engine, generate(WINDOW + 5, device_id="pg-elsewhere", start_id=70_000))
    # The engine is still ready for the device it owns; only the reading was
    # rejected. The backend contract has no third readiness value.
    assert engine.readiness() == "ready"


def test_an_artifact_naming_no_device_is_unavailable(trained_artifact) -> None:
    import json

    from powerguard_ml.artifact import METADATA_FILE

    directory, _ = trained_artifact
    path = directory / METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["device_id"] = ""
    path.write_text(json.dumps(payload), encoding="utf-8")

    engine = IsolationForestInference(directory)
    assert engine.readiness() == UNAVAILABLE
    assert feed(engine, generate(WINDOW + 5)) == [None] * (WINDOW + 5)


# -- malformed metadata reaches the adapter as `unavailable` --------------


def test_malformed_metadata_becomes_unavailable_not_a_crash(trained_artifact) -> None:
    import json

    from powerguard_ml.artifact import METADATA_FILE

    directory, _ = trained_artifact
    path = directory / METADATA_FILE
    original = json.loads(path.read_text(encoding="utf-8"))

    for field, value in (
        ("window", "not-a-number"),
        ("threshold", None),
        ("feature_names", "voltage_v"),
        ("calibration_scores", "0.1,0.2"),
        ("calibration_scores", [0.1, "x"]),
    ):
        path.write_text(json.dumps(original | {field: value}), encoding="utf-8")
        engine = IsolationForestInference(directory)
        assert engine.readiness() == UNAVAILABLE, field
        assert engine.unavailable_reason
        # And it still answers the ingestion path without raising.
        assert feed(engine, generate(WINDOW + 2)) == [None] * (WINDOW + 2)


def test_metadata_that_is_not_json_becomes_unavailable(trained_artifact) -> None:
    from powerguard_ml.artifact import METADATA_FILE

    directory, _ = trained_artifact
    (directory / METADATA_FILE).write_text("{ truncated", encoding="utf-8")
    engine = IsolationForestInference(directory)
    assert engine.readiness() == UNAVAILABLE
    assert "not JSON" in (engine.unavailable_reason or "")


def test_an_undeserialisable_model_becomes_unavailable(trained_artifact) -> None:
    import json

    from powerguard_ml.artifact import METADATA_FILE, MODEL_FILE, _sha256

    directory, _ = trained_artifact
    (directory / MODEL_FILE).write_bytes(b"definitely-not-a-pickle")
    path = directory / METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model_sha256"] = _sha256(directory / MODEL_FILE)
    path.write_text(json.dumps(payload), encoding="utf-8")

    engine = IsolationForestInference(directory)
    assert engine.readiness() == UNAVAILABLE
    assert "deserialised" in (engine.unavailable_reason or "")


def test_the_adapter_reports_the_artifact_it_loaded(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    assert engine.metadata["device_id"] == DEFAULT_DEVICE
    assert engine.metadata["data_quality"] == "quality_not_established"


# -- the seq stride: firmware publishes one of every two samples ------------
#
# The adapter must cut live windows with the stride the artifact was trained
# with. Demanding +1 against real firmware reset the window on every reading,
# so a model loaded against hardware could never score anything.


def _strided(rows, stride: int = 2):
    from dataclasses import replace

    first = rows[0].seq
    return [replace(row, seq=first + stride * index) for index, row in enumerate(rows)]


def _scoring_spy(engine: IsolationForestInference) -> list[int]:
    """Count the windows the engine actually scores, without changing them."""
    model = engine._model
    assert model is not None
    calls: list[int] = []

    class _Spy:
        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(model, name)

        def score(self, matrix):  # type: ignore[no-untyped-def]
            calls.append(len(matrix))
            return model.score(matrix)

    engine._model = _Spy()  # type: ignore[assignment]
    return calls


def test_the_artifact_declares_the_stride_the_adapter_uses(trained_artifact) -> None:
    from powerguard_ml.preprocess import EXPECTED_SEQ_STRIDE

    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    assert engine.metadata["expected_seq_stride"] == EXPECTED_SEQ_STRIDE
    assert engine.expected_seq_stride == EXPECTED_SEQ_STRIDE


def test_a_seq_stride_of_two_fills_a_window_and_is_scored(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    calls = _scoring_spy(engine)
    feed(engine, _strided(generate(WINDOW + 10, boot_id="boot-stride")))
    # Every reading from the 30th on completes a window and is scored.
    assert len(calls) == 11


def test_a_stride_two_departure_is_flagged_end_to_end(trained_artifact) -> None:
    """The real firmware cadence reaches a verdict, not just a full buffer."""
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    rows = _strided(
        generate(
            220,
            seed=33,
            boot_id="boot-stride-spike",
            start_id=80_000,
            scenarios=(Scenario("spike", start=120, length=60),),
        )
    )
    assert any(verdict is not None for verdict in feed(engine, rows))


def test_an_advance_beyond_the_stride_still_resets_the_window(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    calls = _scoring_spy(engine)
    rows = _strided(generate(WINDOW + 20, boot_id="boot-loss"))
    # One lost publish: the reading after it advances by 4, beyond stride 2.
    holed = rows[:WINDOW] + rows[WINDOW + 1 :]
    feed(engine, holed)
    # One window scored before the hole; after it, 30 fresh readings are needed
    # and only 19 arrive, so nothing else is scored.
    assert len(calls) == 1


def test_a_stride_wider_than_declared_is_never_scored(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    calls = _scoring_spy(engine)
    rows = _strided(
        generate(
            220,
            seed=33,
            boot_id="boot-wide",
            start_id=90_000,
            scenarios=(Scenario("spike", start=120, length=60),),
        ),
        stride=3,
    )
    assert feed(engine, rows) == [None] * len(rows)
    assert calls == []


def test_a_stride_one_artifact_treats_an_advance_of_two_as_a_hole(trained_artifact) -> None:
    import json

    from powerguard_ml.artifact import METADATA_FILE

    directory, _ = trained_artifact
    path = directory / METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload | {"expected_seq_stride": 1}), encoding="utf-8")

    engine = IsolationForestInference(directory)
    assert engine.expected_seq_stride == 1
    calls = _scoring_spy(engine)
    feed(engine, _strided(generate(WINDOW + 10, boot_id="boot-strict")))
    assert calls == []
    feed(engine, generate(WINDOW + 10, boot_id="boot-every-sample", start_id=95_000))
    assert len(calls) == 11


def test_a_reboot_resets_the_window_whatever_the_stride(trained_artifact) -> None:
    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    calls = _scoring_spy(engine)
    feed(engine, _strided(generate(WINDOW - 1, boot_id="boot-one")))
    feed(engine, _strided(generate(WINDOW - 1, boot_id="boot-two", start_id=96_000)))
    assert calls == []


def test_a_time_gap_resets_the_window_whatever_the_stride(trained_artifact) -> None:
    from dataclasses import replace

    directory, _ = trained_artifact
    engine = IsolationForestInference(directory)
    calls = _scoring_spy(engine)
    rows = _strided(generate(2 * WINDOW - 2, boot_id="boot-stall"))
    stalled = [
        replace(row, received_at=row.received_at + dt.timedelta(seconds=30))
        if index >= WINDOW - 1
        else row
        for index, row in enumerate(rows)
    ]
    feed(engine, stalled)
    assert calls == []


def test_a_legacy_artifact_without_a_stride_is_unavailable(trained_artifact) -> None:
    import json

    from powerguard_ml.artifact import METADATA_FILE

    directory, _ = trained_artifact
    path = directory / METADATA_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["expected_seq_stride"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    engine = IsolationForestInference(directory)
    # Never guessed: the window rule it was trained with is unknown.
    assert engine.readiness() == UNAVAILABLE
    assert "legacy" in (engine.unavailable_reason or "")
    assert engine.expected_seq_stride is None


def test_metadata_that_is_not_utf8_becomes_unavailable(trained_artifact) -> None:
    from powerguard_ml.artifact import METADATA_FILE

    directory, _ = trained_artifact
    (directory / METADATA_FILE).write_bytes(b'{"window": "\xff\xfe"}')
    engine = IsolationForestInference(directory)
    assert engine.readiness() == UNAVAILABLE
    assert "UnicodeDecodeError" in (engine.unavailable_reason or "")
