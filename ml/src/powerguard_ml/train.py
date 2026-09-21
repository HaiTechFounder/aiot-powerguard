"""`python -m powerguard_ml.train` -- fit one baseline and write one artifact.

No search, no sweep, no leaderboard: `ML_ARCHITECTURE.md` names the pipeline
and its hyperparameters, so this fits exactly that and records what it did.

The split is by time. Training windows come from the first 80% of the prepared
rows and the calibration distribution from the last 20%, so the scores a
threshold is read against were never seen during fitting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from powerguard_ml import dataset, synthetic
from powerguard_ml.artifact import Metadata, artifact_dir, library_versions, now_utc, save
from powerguard_ml.features import build_matrix
from powerguard_ml.model import DEFAULT_CONSECUTIVE, DEFAULT_THRESHOLD, RANDOM_STATE, train
from powerguard_ml.preprocess import prepare, time_split


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="powerguard_ml.train",
        description="Fit the baseline anomaly model and write a local artifact.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path, help="a .jsonl or .csv export")
    source.add_argument(
        "--synthetic",
        type=int,
        metavar="N",
        help="generate N deterministic synthetic rows instead (verification only)",
    )
    parser.add_argument("--device-id", help="train on this device only")
    parser.add_argument("--calibration-fingerprint", help="restrict to one calibration regime")
    parser.add_argument("--model-version", default="v1", help="artifact version directory name")
    parser.add_argument(
        "--artifacts", type=Path, default=Path("artifacts"), help="artifact root directory"
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--consecutive", type=int, default=DEFAULT_CONSECUTIVE)
    parser.add_argument("--train-fraction", type=float, default=0.8)
    parser.add_argument("--random-state", type=int, default=RANDOM_STATE)
    parser.add_argument("--seed", type=int, default=synthetic.DEFAULT_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.synthetic is not None:
        rows = synthetic.training_set(args.synthetic, seed=args.seed)
        provenance = "synthetic"
    else:
        rows = dataset.load(args.dataset)
        provenance = "real_export"

    prepared = prepare(
        rows,
        device_id=args.device_id,
        calibration_fingerprint=args.calibration_fingerprint,
    )
    if not prepared.segments:
        print(
            "no usable segment: fewer than 30 contiguous samples survived preparation",
            file=sys.stderr,
        )
        return 2

    usable = [row for segment in prepared.segments for row in segment]
    split = time_split(usable, train_fraction=args.train_fraction)
    train_matrix, _ = build_matrix(
        prepare(split.train).segments if split.train else ()
    )
    calibration_matrix, _ = build_matrix(
        prepare(split.calibration).segments if split.calibration else ()
    )

    if train_matrix.size == 0 or calibration_matrix.size == 0:
        print(
            "the time split left no complete window on one side; "
            "collect more contiguous samples",
            file=sys.stderr,
        )
        return 2

    model = train(
        train_matrix,
        calibration_matrix,
        threshold=args.threshold,
        consecutive=args.consecutive,
        random_state=args.random_state,
    )

    device_id = args.device_id or usable[0].device_id
    fingerprint = args.calibration_fingerprint or usable[0].calibration_fingerprint
    metadata = Metadata(
        model_version=args.model_version,
        device_id=device_id,
        created_at=now_utc(),
        threshold=args.threshold,
        consecutive=args.consecutive,
        random_state=args.random_state,
        training_samples=len(split.train),
        training_windows=int(train_matrix.shape[0]),
        calibration_windows=int(calibration_matrix.shape[0]),
        training_interval=(
            split.train[0].received_at.isoformat() if split.train else None,
            split.train[-1].received_at.isoformat() if split.train else None,
        ),
        calibration_fingerprint=fingerprint,
        libraries=library_versions(),
        # Synthetic data cannot establish quality, and neither can an
        # unapproved real export. Promotion is a separate, manual decision.
        data_quality="quality_not_established",
        data_provenance=provenance,
    )

    target = artifact_dir(args.artifacts, device_id, args.model_version)
    save(model, metadata, target)

    print(f"artifact: {target}")
    print(f"device: {device_id}  regime: {fingerprint}")
    print(
        f"rows in: {prepared.total_input}  usable: {prepared.kept}  "
        f"segments: {len(prepared.segments)}"
    )
    print(
        f"train windows: {train_matrix.shape[0]}  "
        f"calibration windows: {calibration_matrix.shape[0]}"
    )
    print(f"threshold: {args.threshold}  consecutive: {args.consecutive}")
    print(f"data_quality: quality_not_established  provenance: {provenance}")
    if provenance == "synthetic":
        print(
            "NOTE: synthetic data verifies pipeline mechanics only. "
            "It is not evidence of real-world accuracy."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
