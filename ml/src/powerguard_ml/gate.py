"""The real-data gate: the one thing standing between a fit and a promotion.

Training always works. That is the problem this module exists for -- sklearn
will happily fit 60 rows of off-state synthetic telemetry and hand back an
artifact that looks exactly like a validated one. The gate is what makes the
difference visible, and it is deliberately hard to satisfy by accident:

* at least 1,000 operator-approved **normal** samples,
* from one device -- the one the manifest names,
* from one **named** calibration regime -- the one the manifest names; a
  missing fingerprint is a failure, never a wildcard,
* attested as hardware provenance by a person, with no template placeholder
  left in the attestation,
* from power cycles (`boot_ids`) the operator witnessed and listed,
* with operator-labelled abnormal events to evaluate detection against,
* surviving the approved contiguity rule with enough windows on both sides of
  the chronological split to fit and to calibrate.

When any of these is missing the answer is ``DATA_GATE_BLOCKED``. There is no
override: the only way through is data a person witnessed.

`data_quality` in an artifact's metadata is set from this result and from
nothing else, and the backend refuses to load anything that is not
``validated_real_data`` unless explicitly told otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from powerguard_ml.dataset import Sample
from powerguard_ml.preprocess import prepare
from powerguard_ml.review import Manifest

#: The verdict strings an operator greps for.
DATA_GATE_BLOCKED = "DATA_GATE_BLOCKED"
DATA_GATE_PASSED = "DATA_GATE_PASSED"

#: `review.template` marks every field a person must fill in with this.
PLACEHOLDER_MARK = "REPLACE-ME"

#: Exit code of every CLI that refuses on the gate.
EXIT_BLOCKED = 3

#: The Phase 05 promotion threshold, in operator-approved real samples.
MIN_APPROVED_SAMPLES = 1000

#: Minimum complete windows required on each side of the chronological split.
MIN_TRAIN_WINDOWS = 50
MIN_CALIBRATION_WINDOWS = 20

QUALITY_VALIDATED = "validated_real_data"
QUALITY_NOT_ESTABLISHED = "quality_not_established"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Whether a dataset may produce a promotable artifact, and why not."""

    passed: bool
    approved_samples: int
    #: Approved rows labelled normal -- the only rows a baseline may fit on.
    normal_samples: int
    #: Approved rows inside an operator-labelled abnormal event.
    labelled_abnormal_samples: int
    usable_samples: int
    usable_segments: int
    device_id: str | None
    calibration_fingerprint: str | None
    provenance: str
    reasons: tuple[str, ...] = ()

    @property
    def data_quality(self) -> str:
        return QUALITY_VALIDATED if self.passed else QUALITY_NOT_ESTABLISHED

    @property
    def verdict(self) -> str:
        return DATA_GATE_PASSED if self.passed else DATA_GATE_BLOCKED

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verdict"] = self.verdict
        payload["data_quality"] = self.data_quality
        payload["min_approved_samples"] = MIN_APPROVED_SAMPLES
        return payload


def evaluate_gate(
    samples: Sequence[Sample],
    manifest: Manifest | None,
    *,
    min_approved: int = MIN_APPROVED_SAMPLES,
) -> GateResult:
    """Judge an approved dataset against the promotion requirements."""
    reasons: list[str] = []
    normal = [sample for sample in samples if sample.label in (None, 0)]
    abnormal = [sample for sample in samples if sample.label == 1]

    if manifest is None:
        return GateResult(
            passed=False,
            approved_samples=len(samples),
            normal_samples=len(normal),
            labelled_abnormal_samples=len(abnormal),
            usable_samples=0,
            usable_segments=0,
            device_id=samples[0].device_id if samples else None,
            calibration_fingerprint=None,
            provenance="unattested",
            reasons=(
                "no operator review manifest: nothing has attested that these rows are "
                "real, which regime produced them, or that they are fit to train on",
            ),
        )

    if not manifest.is_hardware:
        reasons.append(
            f"provenance is {manifest.provenance!r}, not 'hardware'; only data an operator "
            "attests came from the device may be promoted"
        )

    # The template is written to fail; a field still carrying its placeholder
    # is an attestation nobody made.
    unfilled = [
        name
        for name, value in (
            ("calibration_fingerprint", manifest.calibration_fingerprint),
            ("firmware_version", manifest.firmware_version),
            ("reviewed_by", manifest.reviewed_by),
        )
        if PLACEHOLDER_MARK in value
    ]
    if unfilled:
        reasons.append(
            f"the manifest still carries template placeholders in: {', '.join(unfilled)}"
        )

    devices = sorted({sample.device_id for sample in samples})
    if len(devices) > 1:
        reasons.append(f"samples span {len(devices)} devices: {', '.join(devices)}")
    elif devices and devices[0] != manifest.device_id:
        reasons.append(
            f"samples come from {devices[0]!r} but the manifest attests to "
            f"{manifest.device_id!r}"
        )

    fingerprints = sorted({sample.calibration_fingerprint or "" for sample in samples})
    named = [fingerprint for fingerprint in fingerprints if fingerprint]
    if "" in fingerprints:
        reasons.append(
            "at least one approved sample carries no calibration fingerprint; a missing "
            "fingerprint is never treated as matching"
        )
    if len(named) > 1:
        reasons.append(
            "samples span more than one calibration regime; an artifact fitted across two "
            "calibrations was fitted on two different sensors"
        )
    elif named and named[0] != manifest.calibration_fingerprint:
        reasons.append(
            f"samples carry calibration regime {named[0]!r} but the manifest attests to "
            f"{manifest.calibration_fingerprint!r}"
        )

    if len(normal) < min_approved:
        reasons.append(
            f"only {len(normal)} operator-approved normal sample(s); at least "
            f"{min_approved} are required before promotion"
        )

    # Witnessing is per power cycle. A manifest that names no boot has not said
    # which capture it vouches for, so it vouches for none.
    boots = sorted({sample.boot_id for sample in samples})
    if not manifest.boot_ids:
        reasons.append(
            "the manifest lists no witnessed boot_ids; name the power cycle(s) you "
            "watched, from the dashboard's Latest Reading panel"
        )
    else:
        unwitnessed = [boot for boot in boots if boot not in manifest.boot_ids]
        if unwitnessed:
            reasons.append(
                f"samples include boot(s) the manifest does not witness: "
                f"{', '.join(unwitnessed)}"
            )

    # Without labelled faults there is nothing to measure detection against,
    # so no recall figure exists and nothing may be called validated.
    if not manifest.labelled_events:
        reasons.append(
            "the manifest labels no abnormal events; induce and label faults so that "
            "detection can be evaluated at all"
        )
    elif not abnormal:
        reasons.append(
            "the manifest labels abnormal events but no approved sample falls inside one; "
            "there is still nothing to evaluate detection against"
        )

    # The contiguity rule is the same one training applies, so the gate cannot
    # pass on rows that training would then throw away. The stride the operator
    # attested to is the one used, so a capture from a differently configured
    # firmware build cannot borrow this build's tolerance.
    prepared = None
    if normal:
        try:
            prepared = prepare(normal, expected_seq_stride=manifest.expected_seq_stride)
        except ValueError as refusal:
            # `prepare` refuses a mixed device or a mixed regime outright. The
            # gate reports that as a blocking finding rather than letting the
            # exception escape: a caller asking "may this be promoted?" is
            # owed an answer, not a traceback.
            reasons.append(str(refusal))
    usable_segments = len(prepared.segments) if prepared else 0
    usable_samples = prepared.kept if prepared else 0

    if usable_samples < min_approved:
        reasons.append(
            f"only {usable_samples} approved normal sample(s) survive the 30-sample "
            f"contiguity rule across {usable_segments} segment(s); "
            f"at least {min_approved} are required"
        )

    # A chronological split needs enough on both sides; a gate that ignores
    # this passes datasets that then fail at fit time.
    if prepared is not None and usable_samples:
        train_rows = int(usable_samples * 0.8)
        approximate_train_windows = max(0, train_rows - 30 + 1)
        approximate_calibration_windows = max(0, (usable_samples - train_rows) - 30 + 1)
        if approximate_train_windows < MIN_TRAIN_WINDOWS:
            reasons.append(
                f"the chronological split would leave about {approximate_train_windows} "
                f"training window(s); at least {MIN_TRAIN_WINDOWS} are required"
            )
        if approximate_calibration_windows < MIN_CALIBRATION_WINDOWS:
            reasons.append(
                f"the chronological split would leave about "
                f"{approximate_calibration_windows} calibration window(s); at least "
                f"{MIN_CALIBRATION_WINDOWS} are required to read a threshold against"
            )

    return GateResult(
        passed=not reasons,
        approved_samples=len(samples),
        normal_samples=len(normal),
        labelled_abnormal_samples=len(abnormal),
        usable_samples=usable_samples,
        usable_segments=usable_segments,
        device_id=devices[0] if len(devices) == 1 else None,
        calibration_fingerprint=manifest.calibration_fingerprint,
        provenance=manifest.provenance,
        reasons=tuple(reasons),
    )


#: What an operator has to do, in order, when the gate says no. Printed with
#: every blocked verdict so the next step is never a matter of reading code.
CHECKLIST: tuple[str, ...] = (
    "Run a witnessed 60-90 minute capture on the real ESP8266 + INA226, load on, "
    "no reboot (ml/REAL_DATA_GATE.md, 'The witnessed capture').",
    "Write down the boot_id, the wall-clock start/end, the firmware build and the "
    "shunt actually fitted.",
    "Induce and time abnormal events (overcurrent, brownout, disconnect) outside "
    "the normal window.",
    "Write ml/review.json from review.template: provenance 'hardware' only if you "
    "witnessed it, a calibration_fingerprint, boot_ids, approved_intervals and "
    "labelled_events. Replace every REPLACE-ME.",
    "Re-run this gate with --manifest until it reports DATA_GATE_PASSED, then "
    "export, train with --promote, evaluate, and run shadow before active.",
)


def render(result: GateResult) -> str:
    lines = [
        f"real-data gate: {result.verdict}",
        f"  device                {result.device_id}",
        f"  calibration regime    {result.calibration_fingerprint}",
        f"  provenance            {result.provenance}",
        f"  approved samples      {result.approved_samples}",
        f"  normal samples        {result.normal_samples} (need {MIN_APPROVED_SAMPLES})",
        f"  labelled abnormal     {result.labelled_abnormal_samples} (need at least 1)",
        f"  usable after prep     {result.usable_samples} "
        f"in {result.usable_segments} segment(s)",
        f"  data_quality          {result.data_quality}",
    ]
    if result.reasons:
        lines.append("  blocked by:")
        lines.extend(f"    - {reason}" for reason in result.reasons)
        lines.append("  to unblock:")
        lines.extend(
            f"    {index}. {step}" for index, step in enumerate(CHECKLIST, start=1)
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m powerguard_ml.gate` -- may this data be promoted? Never writes a model."""
    from powerguard_ml import dataset, review, sqlite_source

    parser = argparse.ArgumentParser(
        prog="powerguard_ml.gate",
        description=(
            f"Judge data against the real-data gate. Exits 0 on {DATA_GATE_PASSED}, "
            f"{EXIT_BLOCKED} on {DATA_GATE_BLOCKED}. Reads only; trains nothing."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", type=Path, help="the backend SQLite database")
    source.add_argument("--dataset", type=Path, help="a .jsonl or .csv export")
    parser.add_argument("--manifest", type=Path, help="operator review manifest")
    parser.add_argument("--device-id", help="database only: read this device")
    parser.add_argument("--json", type=Path, help="also write the verdict as JSON")
    args = parser.parse_args(argv)

    try:
        manifest = review.load_manifest(args.manifest) if args.manifest else None
        if args.database is not None:
            device = manifest.device_id if manifest else args.device_id
            rows = sqlite_source.read_telemetry(args.database, device_id=device)
        else:
            rows = dataset.load(args.dataset)
    except ValueError as failure:
        print(f"{DATA_GATE_BLOCKED}: {failure}", file=sys.stderr)
        return EXIT_BLOCKED
    # A manifest approves rows; it never vouches for rows outside its intervals.
    approved = review.apply_manifest(rows, manifest) if manifest else rows

    result = evaluate_gate(approved, manifest)
    print(render(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    return 0 if result.passed else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
