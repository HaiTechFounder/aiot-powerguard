"""`powerguard_features_v1`: thirteen numbers from a 30-sample window.

Feature order, units, window length and formulas are immutable metadata. An
artifact records them and inference must reproduce them exactly — a reordered
vector is not a different opinion, it is a different model being asked the
wrong question. `FEATURE_VERSION` changes the moment any of this does.

Cumulative `energy_wh` is deliberately absent: it only ever increases, so a
model trained on it learns the clock rather than the load.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from powerguard_ml.dataset import Sample

FEATURE_VERSION = "powerguard_features_v1"
WINDOW = 30

#: The exact order of a feature vector. Never reorder; bump the version.
FEATURE_NAMES: tuple[str, ...] = (
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

FEATURE_COUNT = len(FEATURE_NAMES)


class WindowUnavailable(ValueError):
    """Fewer than `WINDOW` contiguous samples; no vector can be built."""


def window_vector(window: Sequence[Sample], *, size: int = WINDOW) -> np.ndarray:
    """One feature vector from the trailing `size` samples of one segment.

    The caller guarantees contiguity — `preprocess.prepare` is what establishes
    it — because nothing in a bare list of rows can prove it.
    """
    if len(window) < size:
        raise WindowUnavailable(f"need {size} contiguous samples, got {len(window)}")
    tail = window[-size:]

    voltage = np.fromiter((row.voltage_v for row in tail), dtype=np.float64, count=size)
    current = np.fromiter((row.current_a for row in tail), dtype=np.float64, count=size)
    power = np.fromiter((row.power_w for row in tail), dtype=np.float64, count=size)

    latest = tail[-1]
    previous = tail[-2]
    return np.array(
        [
            latest.voltage_v,
            latest.current_a,
            latest.power_w,
            latest.voltage_v - previous.voltage_v,
            latest.current_a - previous.current_a,
            latest.power_w - previous.power_w,
            float(voltage.mean()),
            float(current.mean()),
            float(power.mean()),
            float(voltage.std()),
            float(current.std()),
            float(power.std()),
            abs(latest.power_w - latest.voltage_v * latest.current_a),
        ],
        dtype=np.float64,
    )


def segment_matrix(
    segment: Sequence[Sample], *, size: int = WINDOW
) -> tuple[np.ndarray, list[Sample]]:
    """Every complete trailing window in one contiguous segment.

    Returns the matrix and the sample each row is a verdict *about* — the
    newest in its window — so a score can be traced back to a reading.
    """
    if len(segment) < size:
        return np.empty((0, FEATURE_COUNT), dtype=np.float64), []
    vectors: list[np.ndarray] = []
    anchors: list[Sample] = []
    for end in range(size, len(segment) + 1):
        vectors.append(window_vector(segment[end - size : end], size=size))
        anchors.append(segment[end - 1])
    return np.vstack(vectors), anchors


def build_matrix(
    segments: Sequence[Sequence[Sample]], *, size: int = WINDOW
) -> tuple[np.ndarray, list[Sample]]:
    """Windows across many segments; a window never spans a segment boundary."""
    matrices: list[np.ndarray] = []
    anchors: list[Sample] = []
    for segment in segments:
        matrix, rows = segment_matrix(segment, size=size)
        if matrix.size:
            matrices.append(matrix)
            anchors.extend(rows)
    if not matrices:
        return np.empty((0, FEATURE_COUNT), dtype=np.float64), []
    return np.vstack(matrices), anchors
