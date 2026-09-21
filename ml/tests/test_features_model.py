"""The feature contract, and the scoring that reads a threshold."""

from __future__ import annotations

import numpy as np
import pytest

from powerguard_ml.features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    FEATURE_VERSION,
    WINDOW,
    WindowUnavailable,
    build_matrix,
    segment_matrix,
    window_vector,
)
from powerguard_ml.model import DEFAULT_THRESHOLD, normalise, suppress, train
from powerguard_ml.preprocess import prepare, time_split
from powerguard_ml.synthetic import Scenario, generate, training_set


def test_the_feature_contract_is_pinned() -> None:
    # Reordering or renaming here invalidates every existing artifact, so it
    # must be a deliberate version bump, not an incidental edit.
    assert FEATURE_VERSION == "powerguard_features_v1"
    assert WINDOW == 30
    assert FEATURE_NAMES == (
        "voltage_v",
        "current_a",
        "power_w",
        "delta_voltage_v",
        "delta_current_a",
        "delta_power_w",
        "mean_voltage_v",
        "mean_current_a",
        "mean_power_w",
        "std_voltage_v",
        "std_current_a",
        "std_power_w",
        "power_residual",
    )


def test_cumulative_energy_is_never_a_feature() -> None:
    assert not any("energy" in name for name in FEATURE_NAMES)


def test_a_short_window_yields_no_vector() -> None:
    with pytest.raises(WindowUnavailable):
        window_vector(generate(29))


def test_the_vector_computes_what_the_contract_says() -> None:
    rows = generate(WINDOW)
    vector = window_vector(rows)
    assert vector.shape == (FEATURE_COUNT,)

    latest, previous = rows[-1], rows[-2]
    voltages = np.array([row.voltage_v for row in rows])
    assert vector[0] == pytest.approx(latest.voltage_v)
    assert vector[3] == pytest.approx(latest.voltage_v - previous.voltage_v)
    assert vector[6] == pytest.approx(voltages.mean())
    assert vector[9] == pytest.approx(voltages.std())
    assert vector[12] == pytest.approx(abs(latest.power_w - latest.voltage_v * latest.current_a))


def test_every_trailing_window_is_produced_once() -> None:
    rows = generate(45)
    matrix, anchors = segment_matrix(rows)
    assert matrix.shape == (45 - WINDOW + 1, FEATURE_COUNT)
    # A score belongs to the newest reading in its window.
    assert anchors[0].id == rows[WINDOW - 1].id
    assert anchors[-1].id == rows[-1].id


def test_a_window_never_spans_a_segment_boundary() -> None:
    first, second = generate(35), generate(35, boot_id="boot-b", start_id=900, start_seq=1)
    matrix, _ = build_matrix([first, second])
    assert matrix.shape[0] == 2 * (35 - WINDOW + 1)


def test_scoring_is_deterministic_across_identical_runs() -> None:
    rows = training_set(400)
    split = time_split([row for segment in prepare(rows).segments for row in segment])
    train_matrix, _ = build_matrix(prepare(split.train).segments)
    calibration_matrix, _ = build_matrix(prepare(split.calibration).segments)

    first = train(train_matrix, calibration_matrix)
    second = train(train_matrix, calibration_matrix)
    assert np.array_equal(first.score(calibration_matrix), second.score(calibration_matrix))


def test_a_normalised_score_is_a_percentile_in_zero_to_one() -> None:
    calibration = np.arange(100, dtype=float)
    assert normalise(np.array([-1.0]), calibration)[0] == 0.0
    assert normalise(np.array([99.0]), calibration)[0] == 1.0
    assert normalise(np.array([49.0]), calibration)[0] == pytest.approx(0.5)


def test_suppression_needs_two_consecutive_windows() -> None:
    above = np.array([False, True, False, True, True, True, False])
    assert list(suppress(above, 2)) == [False, False, False, False, True, True, False]


def test_one_isolated_extreme_window_is_not_a_verdict() -> None:
    assert not suppress(np.array([True]), 2).any()


def test_training_needs_both_sides_of_the_split() -> None:
    empty = np.empty((0, FEATURE_COUNT))
    matrix, _ = build_matrix(prepare(generate(60)).segments)
    with pytest.raises(ValueError, match="no training windows"):
        train(empty, matrix)
    with pytest.raises(ValueError, match="no calibration windows"):
        train(matrix, empty)


def test_an_injected_departure_scores_above_nominal() -> None:
    # A behavioural check on a synthetic fixture: it proves the pipeline
    # reacts, and proves nothing at all about a real load.
    rows = training_set(500)
    split = time_split([row for segment in prepare(rows).segments for row in segment])
    model = train(
        build_matrix(prepare(split.train).segments)[0],
        build_matrix(prepare(split.calibration).segments)[0],
    )

    nominal, _ = build_matrix(prepare(generate(120, seed=7, boot_id="boot-n")).segments)
    disturbed, _ = build_matrix(
        prepare(
            generate(
                120,
                seed=7,
                boot_id="boot-d",
                start_id=5000,
                scenarios=(Scenario("spike", start=60, length=30),),
            )
        ).segments
    )
    assert disturbed.shape[0] == nominal.shape[0]
    assert model.score(disturbed).max() > model.score(nominal).max()
    assert model.threshold == DEFAULT_THRESHOLD
