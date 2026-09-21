"""The one baseline: RobustScaler -> IsolationForest, with a calibrated score.

A raw IsolationForest score has no units anyone can reason about, so it is
never the thing a threshold is applied to. The final 20% of the training window
— held out in *time* — supplies a distribution, and an online score is that
raw value's empirical percentile within it. `0.99` then means what it sounds
like: more extreme than 99% of the calibration data.

A single above-threshold window is not a verdict. Consecutive-window
suppression exists because one noisy sample at a 2-second cadence is noise,
and two in a row is a signal — `ML_ARCHITECTURE.md` §Model and scoring.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler

MODEL_KIND = "robust_scaler+isolation_forest"
RANDOM_STATE = 42
N_ESTIMATORS = 200
DEFAULT_THRESHOLD = 0.99
#: Above-threshold windows required in a row before a verdict is emitted.
DEFAULT_CONSECUTIVE = 2


def build_pipeline(
    *, n_estimators: int = N_ESTIMATORS, random_state: int = RANDOM_STATE
) -> Pipeline:
    """The fixed baseline. No search: the spec names these values."""
    return Pipeline(
        [
            ("scaler", RobustScaler()),
            (
                "forest",
                IsolationForest(
                    n_estimators=n_estimators,
                    random_state=random_state,
                    n_jobs=1,
                ),
            ),
        ]
    )


def raw_scores(pipeline: Pipeline, matrix: np.ndarray) -> np.ndarray:
    """Larger means more anomalous, which `score_samples` alone does not give."""
    if matrix.size == 0:
        return np.empty((0,), dtype=np.float64)
    return -np.asarray(pipeline.score_samples(matrix), dtype=np.float64)


def normalise(raw: np.ndarray | float, calibration: np.ndarray) -> np.ndarray:
    """Empirical percentile of `raw` within the sorted calibration scores.

    `side="right"` makes the score the fraction of calibration values at or
    below the observation, so the largest calibration score maps to 1.0 rather
    than to something just short of it.
    """
    sorted_calibration = np.asarray(calibration, dtype=np.float64)
    values = np.atleast_1d(np.asarray(raw, dtype=np.float64))
    if sorted_calibration.size == 0:
        return np.zeros_like(values)
    position = np.searchsorted(sorted_calibration, values, side="right")
    return position.astype(np.float64) / float(sorted_calibration.size)


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """A fitted pipeline and the distribution its scores are read against."""

    pipeline: Pipeline
    #: Ascending raw scores from the held-out calibration split.
    calibration_scores: np.ndarray
    threshold: float = DEFAULT_THRESHOLD
    consecutive: int = DEFAULT_CONSECUTIVE

    def score(self, matrix: np.ndarray) -> np.ndarray:
        """Normalised `0..1` scores, one per feature vector."""
        return normalise(raw_scores(self.pipeline, matrix), self.calibration_scores)

    def flags(self, matrix: np.ndarray) -> np.ndarray:
        """Per-window verdicts after consecutive-window suppression."""
        return suppress(self.score(matrix) >= self.threshold, self.consecutive)


def suppress(above: np.ndarray, consecutive: int) -> np.ndarray:
    """True only where `consecutive` above-threshold windows end at that index."""
    flags = np.zeros_like(above, dtype=bool)
    run = 0
    for index, value in enumerate(above):
        run = run + 1 if value else 0
        flags[index] = run >= consecutive
    return flags


def train(
    train_matrix: np.ndarray,
    calibration_matrix: np.ndarray,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    consecutive: int = DEFAULT_CONSECUTIVE,
    n_estimators: int = N_ESTIMATORS,
    random_state: int = RANDOM_STATE,
) -> TrainedModel:
    """Fit on the training window, calibrate on the later held-out window."""
    if train_matrix.size == 0:
        raise ValueError("no training windows; the dataset is too short or too fragmented")
    if calibration_matrix.size == 0:
        raise ValueError("no calibration windows; the held-out split produced nothing to score")
    pipeline = build_pipeline(n_estimators=n_estimators, random_state=random_state)
    pipeline.fit(train_matrix)
    calibration = np.sort(raw_scores(pipeline, calibration_matrix))
    return TrainedModel(
        pipeline=pipeline,
        calibration_scores=calibration,
        threshold=threshold,
        consecutive=consecutive,
    )
