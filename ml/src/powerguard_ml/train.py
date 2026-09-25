"""`python -m powerguard_ml.train` -- fit one baseline and write one artifact.

No search, no sweep, no leaderboard: `ML_ARCHITECTURE.md` names the pipeline
and its hyperparameters, so this fits exactly that and records what it did.

The split is by time. Training windows come from the first 80% of the prepared
rows and the calibration distribution from the last 20%, so the scores a
threshold is read against were never seen during fitting.

Every `prepare` call uses one stride -- the manifest's attested value, or the
documented firmware default -- and that stride is written into the artifact,
so the backend adapter later cuts live windows by exactly the same rule.
Rows an operator labelled abnormal are held out of the fit: a baseline trained
on the faults it is meant to find would learn them as normal.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from powerguard_ml import dataset, review, synthetic
from powerguard_ml import gate as gate_module
from powerguard_ml.artifact import (
    SHUNT_RESISTANCE_OHM,
    Metadata,
    artifact_dir,
    library_versions,
    now_utc,
    save,
)
from powerguard_ml.features import build_matrix
from powerguard_ml.model import DEFAULT_CONSECUTIVE, DEFAULT_THRESHOLD, RANDOM_STATE, train
from powerguard_ml.preprocess import EXPECTED_SEQ_STRIDE, prepare, time_split


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
    parser.add_argument(
        "--manifest",
        type=Path,
        help="operator review manifest; required for a promotable artifact",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help=(
            "mark the artifact validated_real_data. Refused unless the real-data "
            "gate passes; there is no flag that overrides it."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.promote and args.synthetic is not None:
        # Refused before anything is generated: no manifest, gate result or
        # flag turns generated rows into evidence about a real load.
        print(
            f"{gate_module.DATA_GATE_BLOCKED}: --promote is refused for --synthetic data. "
            "Synthetic rows verify pipeline mechanics only and can never be promoted.",
            file=sys.stderr,
        )
        return gate_module.EXIT_BLOCKED

    manifest = review.load_manifest(args.manifest) if args.manifest else None
    stride = manifest.expected_seq_stride if manifest else EXPECTED_SEQ_STRIDE

    if args.synthetic is not None:
        rows = synthetic.training_set(args.synthetic, seed=args.seed)
        provenance = "synthetic"
    else:
        rows = dataset.load(args.dataset)
        provenance = manifest.provenance if manifest else "real_export"
        if manifest is not None:
            # Only rows the manifest approves, stamped with its regime and
            # labels. Re-applying it to an approved export changes nothing.
            rows = review.apply_manifest(rows, manifest)

    # The gate is evaluated on every run, not only when --promote is passed,
    # so the reasons an artifact is unpromotable are visible before somebody
    # tries to promote it.
    gate_result = gate_module.evaluate_gate(rows, manifest)
    if args.promote and not gate_result.passed:
        print(gate_module.render(gate_result), file=sys.stderr)
        print(
            "DATA_GATE_BLOCKED: refusing to write a promotable artifact. "
            "Fix the findings above and re-run.",
            file=sys.stderr,
        )
        return gate_module.EXIT_BLOCKED

    # Labelled faults are for evaluation, never for the baseline.
    normal_rows = [row for row in rows if row.label != 1]
    prepared = prepare(
        normal_rows,
        device_id=args.device_id or (manifest.device_id if manifest else None),
        calibration_fingerprint=args.calibration_fingerprint,
        expected_seq_stride=stride,
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
        prepare(split.train, expected_seq_stride=stride).segments if split.train else ()
    )
    calibration_matrix, _ = build_matrix(
        prepare(split.calibration, expected_seq_stride=stride).segments
        if split.calibration
        else ()
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
        expected_seq_stride=stride,
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
        # The regime's identity, as attested rather than as assumed.
        shunt_resistance_ohm=(
            manifest.shunt_resistance_ohm if manifest else SHUNT_RESISTANCE_OHM
        ),
        libraries=library_versions(),
        # Quality is earned from the gate and from nothing else. Without
        # --promote the artifact stays unpromotable even if the gate passed,
        # because promotion is a decision a person makes.
        data_quality=(
            gate_module.QUALITY_VALIDATED
            if (args.promote and gate_result.passed)
            else gate_module.QUALITY_NOT_ESTABLISHED
        ),
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
    print(f"expected_seq_stride: {stride}")
    print(f"data_quality: {metadata.data_quality}  provenance: {provenance}")
    print()
    print(gate_module.render(gate_result))
    if provenance == "synthetic":
        print(
            "NOTE: synthetic data verifies pipeline mechanics only. "
            "It is not evidence of real-world accuracy."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
