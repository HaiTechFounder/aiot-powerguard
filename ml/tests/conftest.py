"""Shared fixtures, and the path that makes the backend importable.

The adapter is only meaningful against the backend's real `Telemetry` and
`AnomalyVerdict`, so the compatibility tests import them from `backend/src`
directly. The domain layer is standard-library only, so this needs no backend
dependencies installed -- which is exactly why that layer is kept pure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


@pytest.fixture
def trained_artifact(tmp_path: Path):
    """A real fit over the deterministic fixture, saved and ready to load."""
    from powerguard_ml.artifact import Metadata, now_utc, save
    from powerguard_ml.features import build_matrix
    from powerguard_ml.model import train
    from powerguard_ml.preprocess import prepare, time_split
    from powerguard_ml.synthetic import CALIBRATION_FINGERPRINT, DEFAULT_DEVICE, training_set

    rows = training_set(600)
    split = time_split([row for segment in prepare(rows).segments for row in segment])
    train_matrix, _ = build_matrix(prepare(split.train).segments)
    calibration_matrix, _ = build_matrix(prepare(split.calibration).segments)
    model = train(train_matrix, calibration_matrix)

    directory = tmp_path / DEFAULT_DEVICE / "v1"
    save(
        model,
        Metadata(
            model_version="v1",
            device_id=DEFAULT_DEVICE,
            created_at=now_utc(),
            calibration_fingerprint=CALIBRATION_FINGERPRINT,
        ),
        directory,
    )
    return directory, model
