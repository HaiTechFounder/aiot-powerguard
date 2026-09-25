"""`python -m powerguard_ml.evaluate` -- what the model did, and on what.

The report is counts, bounds and behaviour on injected scenarios. It is
deliberately not a scorecard: on synthetic data, recall against injected
scenarios measures the generator, and `data_quality` says so in the output
rather than in a footnote nobody reads.

`quality_not_established` is the only honest verdict until enough
operator-approved real samples from one calibration regime exist. The report
prints that field whether or not anyone asked for it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from powerguard_ml import dataset, synthetic
from powerguard_ml.artifact import load as load_artifact
from powerguard_ml.artifact import stride_of
from powerguard_ml.features import build_matrix
from powerguard_ml.model import TrainedModel
from powerguard_ml.preprocess import EXPECTED_SEQ_STRIDE, prepare

#: Enough operator-approved real samples to even consider promotion.
REAL_SAMPLE_GATE = 1_000


@dataclass(slots=True)
class Report:
    device_id: str
    model_version: str
    data_provenance: str
    windows: int
    samples_in: int
    samples_usable: int
    segments: int
    time_bounds: tuple[str | None, str | None]
    threshold: float
    consecutive: int
    flagged_windows: int
    #: Share of windows flagged where no scenario was injected.
    normal_flag_rate: float
    scenario_recall: dict[str, float] = field(default_factory=dict)
    score_quantiles: dict[str, float] = field(default_factory=dict)
    data_quality: str = "quality_not_established"
    caveat: str = (
        "Counts and rates describe behaviour on THIS dataset only. On synthetic "
        "data they measure the generator, not real-world accuracy. Production "
        "calibration and promotion remain DEFERRED until at least "
        f"{REAL_SAMPLE_GATE} operator-approved real samples from one calibration "
        "regime exist."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model_version": self.model_version,
            "data_provenance": self.data_provenance,
            "windows": self.windows,
            "samples_in": self.samples_in,
            "samples_usable": self.samples_usable,
            "segments": self.segments,
            "time_bounds": list(self.time_bounds),
            "threshold": self.threshold,
            "consecutive": self.consecutive,
            "flagged_windows": self.flagged_windows,
            "normal_flag_rate": self.normal_flag_rate,
            "scenario_recall": self.scenario_recall,
            "score_quantiles": self.score_quantiles,
            "data_quality": self.data_quality,
            "caveat": self.caveat,
        }


def evaluate_model(
    model: TrainedModel,
    rows: list[Any],
    *,
    device_id: str,
    model_version: str,
    provenance: str,
    scenarios: tuple[synthetic.Scenario, ...] = (),
    expected_seq_stride: int = EXPECTED_SEQ_STRIDE,
) -> Report:
    # Windows are cut with the artifact's own stride, exactly as training cut them.
    prepared = prepare(
        rows,
        device_id=device_id if provenance == "real_export" else None,
        expected_seq_stride=expected_seq_stride,
    )
    matrix, anchors = build_matrix(prepared.segments)
    if matrix.size == 0:
        raise ValueError("no complete window in the evaluation dataset")

    scores = model.score(matrix)
    flags = model.flags(matrix)

    # A window's label is its anchor row's label: the reading being judged.
    labels = np.array([1 if getattr(row, "label", 0) else 0 for row in anchors], dtype=int)
    normal = labels == 0
    normal_flag_rate = float(flags[normal].mean()) if normal.any() else 0.0

    # A scenario is defined by position in the generated series, so anchors are
    # mapped back by row id rather than by any assumption about ordering.
    position = {row.id: index for index, row in enumerate(rows)}
    recall: dict[str, float] = {}
    for scenario in scenarios:
        covered = np.array(
            [scenario.covers(position.get(row.id, -1)) for row in anchors],
            dtype=bool,
        )
        if covered.any():
            recall[scenario.kind] = float(flags[covered].mean())

    return Report(
        device_id=device_id,
        model_version=model_version,
        data_provenance=provenance,
        windows=int(matrix.shape[0]),
        samples_in=prepared.total_input,
        samples_usable=prepared.kept,
        segments=len(prepared.segments),
        time_bounds=(
            anchors[0].received_at.isoformat(),
            anchors[-1].received_at.isoformat(),
        ),
        threshold=model.threshold,
        consecutive=model.consecutive,
        flagged_windows=int(flags.sum()),
        normal_flag_rate=normal_flag_rate,
        scenario_recall=recall,
        score_quantiles={
            "p50": float(np.quantile(scores, 0.50)),
            "p95": float(np.quantile(scores, 0.95)),
            "p99": float(np.quantile(scores, 0.99)),
            "max": float(scores.max()),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="powerguard_ml.evaluate",
        description="Report what a trained artifact does on a dataset.",
    )
    parser.add_argument("--artifact", type=Path, required=True, help="artifact directory")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path, help="a .jsonl or .csv export")
    source.add_argument(
        "--synthetic",
        type=int,
        metavar="N",
        help="generate N deterministic rows with injected scenarios (verification only)",
    )
    parser.add_argument("--seed", type=int, default=synthetic.DEFAULT_SEED + 1)
    parser.add_argument("--json", type=Path, help="also write the report as JSON here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        model, metadata = load_artifact(args.artifact)
    except (ValueError, OSError) as failure:
        print(f"artifact rejected: {failure}", file=sys.stderr)
        return 2

    scenarios: tuple[synthetic.Scenario, ...] = ()
    if args.synthetic is not None:
        rows, scenarios = synthetic.evaluation_set(args.synthetic, seed=args.seed)
        provenance = "synthetic"
    else:
        rows = dataset.load(args.dataset)
        provenance = "real_export"

    try:
        report = evaluate_model(
            model,
            rows,
            device_id=str(metadata.get("device_id", "")),
            model_version=str(metadata.get("model_version", "")),
            provenance=provenance,
            scenarios=scenarios,
            expected_seq_stride=stride_of(metadata),
        )
    except ValueError as failure:
        print(f"evaluation failed: {failure}", file=sys.stderr)
        return 2

    payload = report.as_dict()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"device: {report.device_id}  model: {report.model_version}")
    print(f"provenance: {report.data_provenance}  data_quality: {report.data_quality}")
    print(f"samples in: {report.samples_in}  usable: {report.samples_usable}")
    print(f"segments: {report.segments}  windows: {report.windows}")
    print(f"time bounds: {report.time_bounds[0]} .. {report.time_bounds[1]}")
    print(f"threshold: {report.threshold}  consecutive: {report.consecutive}")
    print(f"flagged windows: {report.flagged_windows}")
    print(f"flag rate on unlabelled-normal windows: {report.normal_flag_rate:.4f}")
    for kind, value in sorted(report.scenario_recall.items()):
        print(f"injected {kind}: window recall {value:.4f}")
    print(f"score quantiles: {report.score_quantiles}")
    print(report.caveat)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
