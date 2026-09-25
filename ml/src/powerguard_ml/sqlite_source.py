"""Reading the backend's own SQLite database, and reporting what is in it.

Two jobs, deliberately kept apart:

* **audit** -- describe the database as it actually is, including the things
  that disqualify it. An audit that only counts rows is how an unusable
  dataset gets trained on.
* **export** -- turn approved rows into the dataset schema, reproducibly.

The telemetry table carries **no calibration fingerprint and no provenance
column**. That is a fact about the schema, not an oversight to be papered
over: nothing in this module may invent either value. An exported row's
fingerprint comes from an operator review manifest or it stays ``None``, and a
``None`` fingerprint never counts as "the same regime". The same applies to
provenance -- the backend accepts hardware and `publish_synthetic.py` over one
identical MQTT contract, so a row in this database cannot prove which produced
it. Only an operator can attest to that, which is what the manifest is for.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from powerguard_ml.dataset import Sample
from powerguard_ml.preprocess import (
    CADENCE_SECONDS,
    EXPECTED_SEQ_STRIDE,
    is_contiguous,
)

#: Columns the backend's `telemetry` table is required to expose.
TELEMETRY_COLUMNS: tuple[str, ...] = (
    "id",
    "device_id",
    "boot_id",
    "seq",
    "received_at",
    "voltage_v",
    "current_a",
    "power_w",
    "energy_wh",
    "sensor_status",
)

#: Columns that would carry the two facts this pipeline needs and the schema
#: does not record. Their absence is reported, never assumed away.
ATTESTATION_COLUMNS: tuple[str, ...] = ("calibration_fingerprint", "provenance")


class SqliteSourceError(ValueError):
    """The database cannot be read as a telemetry source."""


@dataclass(frozen=True, slots=True)
class BootSummary:
    """One power cycle, described."""

    boot_id: str
    samples: int
    first_received_at: str
    last_received_at: str
    span_seconds: float
    median_cadence_seconds: float
    #: How `seq` advances between consecutive stored rows, most common first.
    seq_stride_counts: dict[str, int]
    mean_voltage_v: float
    mean_current_a: float
    off_state_samples: int


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    device_id: str
    samples: int
    boots: int
    first_received_at: str
    last_received_at: str
    sensor_status_counts: dict[str, int]
    off_state_samples: int
    #: Segments a 30-sample window may legitimately span, under the approved
    #: contiguity rule (same boot, `seq` advance within the expected stride,
    #: a positive time gap no wider than the tolerance).
    contiguous_segments: int
    usable_segments: int
    usable_samples: int
    longest_segment: int
    boot_summaries: tuple[BootSummary, ...]


@dataclass(frozen=True, slots=True)
class Audit:
    database: str
    generated_at: str
    total_samples: int
    total_devices: int
    stored_anomalies: int
    #: False whenever the schema cannot tell one calibration regime from another.
    calibration_fingerprint_available: bool
    provenance_recorded: bool
    devices: tuple[DeviceSummary, ...] = ()
    #: Every reason this database, on its own, cannot support promotion.
    blocking_findings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _connect(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    if not target.exists():
        raise SqliteSourceError(f"no database at {target}")
    # Read-only: an audit must never be able to alter the evidence.
    connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(str(row["name"]) for row in rows)


def _parse_time(value: object) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return stamp.astimezone(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def read_telemetry(
    path: Path | str,
    *,
    device_id: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> list[Sample]:
    """Every stored reading, in the backend's own order (ADR-006).

    `calibration_fingerprint` is left `None` because the table does not record
    one. `review.apply_manifest` is the only thing allowed to set it.
    """
    connection = _connect(path)
    try:
        present = _columns(connection, "telemetry")
        if not present:
            raise SqliteSourceError(f"{path}: no telemetry table")
        missing = [name for name in TELEMETRY_COLUMNS if name not in present]
        if missing:
            raise SqliteSourceError(
                f"{path}: telemetry is missing column(s): {', '.join(missing)}"
            )

        clauses: list[str] = []
        params: list[object] = []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(device_id)
        if since is not None:
            clauses.append("received_at >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("received_at < ?")
            params.append(_iso(until))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = connection.execute(
            f"SELECT {', '.join(TELEMETRY_COLUMNS)} FROM telemetry{where} "
            "ORDER BY received_at, id",
            params,
        ).fetchall()
    finally:
        connection.close()

    return [
        Sample(
            id=int(row["id"]),
            device_id=str(row["device_id"]),
            boot_id=str(row["boot_id"]),
            seq=int(row["seq"]),
            received_at=_parse_time(row["received_at"]),
            voltage_v=float(row["voltage_v"]),
            current_a=float(row["current_a"]),
            power_w=float(row["power_w"]),
            energy_wh=float(row["energy_wh"]),
            sensor_status=str(row["sensor_status"]),
            label=None,
            calibration_fingerprint=None,
        )
        for row in rows
    ]


def is_off_state(
    sample: Sample, *, min_voltage_v: float = 1.0, min_current_a: float = 0.001
) -> bool:
    """A reading taken with the load effectively off.

    Off-state rows are real and valid, and they are still the wrong training
    data: a detector fitted on them learns that "nothing happening" is the
    norm. They are counted here and excluded by the manifest, not deleted.
    """
    return sample.voltage_v < min_voltage_v or sample.current_a <= min_current_a


def _segments(
    samples: Sequence[Sample],
    *,
    window: int = 30,
    expected_seq_stride: int = EXPECTED_SEQ_STRIDE,
) -> list[list[Sample]]:
    """Cut on the rule `preprocess.prepare` uses -- the same predicate, not a copy."""
    if not samples:
        return []
    out: list[list[Sample]] = []
    current: list[Sample] = [samples[0]]
    for previous, nxt in pairwise(samples):
        if is_contiguous(previous, nxt, expected_seq_stride=expected_seq_stride):
            current.append(nxt)
        else:
            out.append(current)
            current = [nxt]
    out.append(current)
    del window
    return out


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _boot_summary(boot_id: str, rows: Sequence[Sample]) -> BootSummary:
    gaps = [
        (b.received_at - a.received_at).total_seconds() for a, b in pairwise(rows)
    ]
    strides = Counter(b.seq - a.seq for a, b in pairwise(rows))
    return BootSummary(
        boot_id=boot_id,
        samples=len(rows),
        first_received_at=_iso(rows[0].received_at),
        last_received_at=_iso(rows[-1].received_at),
        span_seconds=(rows[-1].received_at - rows[0].received_at).total_seconds(),
        median_cadence_seconds=_median(gaps),
        seq_stride_counts={
            str(stride): count for stride, count in sorted(strides.most_common(5))
        },
        mean_voltage_v=sum(row.voltage_v for row in rows) / len(rows),
        mean_current_a=sum(row.current_a for row in rows) / len(rows),
        off_state_samples=sum(1 for row in rows if is_off_state(row)),
    )


def _device_summary(
    device_id: str, rows: Sequence[Sample], *, expected_seq_stride: int = EXPECTED_SEQ_STRIDE
) -> DeviceSummary:
    segments = _segments(rows, expected_seq_stride=expected_seq_stride)
    usable = [segment for segment in segments if len(segment) >= 30]
    boots: dict[str, list[Sample]] = {}
    for row in rows:
        boots.setdefault(row.boot_id, []).append(row)
    return DeviceSummary(
        device_id=device_id,
        samples=len(rows),
        boots=len(boots),
        first_received_at=_iso(rows[0].received_at),
        last_received_at=_iso(rows[-1].received_at),
        sensor_status_counts=dict(Counter(row.sensor_status for row in rows)),
        off_state_samples=sum(1 for row in rows if is_off_state(row)),
        contiguous_segments=len(segments),
        usable_segments=len(usable),
        usable_samples=sum(len(segment) for segment in usable),
        longest_segment=max((len(segment) for segment in segments), default=0),
        boot_summaries=tuple(
            _boot_summary(boot_id, boot_rows) for boot_id, boot_rows in sorted(boots.items())
        ),
    )


def audit(path: Path | str) -> Audit:
    """Describe the database, including everything that disqualifies it."""
    connection = _connect(path)
    try:
        telemetry_columns = _columns(connection, "telemetry")
        if not telemetry_columns:
            raise SqliteSourceError(f"{path}: no telemetry table")
        anomalies = 0
        if _columns(connection, "anomalies"):
            anomalies = int(connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0])
    finally:
        connection.close()

    samples = read_telemetry(path)
    by_device: dict[str, list[Sample]] = {}
    for sample in samples:
        by_device.setdefault(sample.device_id, []).append(sample)

    fingerprint_available = "calibration_fingerprint" in telemetry_columns
    provenance_recorded = "provenance" in telemetry_columns

    findings: list[str] = []
    if not fingerprint_available:
        findings.append(
            "the telemetry schema records no calibration_fingerprint, so this database "
            "cannot by itself prove that any two rows share one calibration regime"
        )
    if not provenance_recorded:
        findings.append(
            "the telemetry schema records no provenance, and hardware and "
            "scripts/publish_synthetic.py publish over the same MQTT contract, so no row "
            "here can be certified as real device data without an operator attestation"
        )
    if anomalies == 0:
        findings.append(
            "no stored anomaly rows, so there are no operator-labelled abnormal events "
            "to evaluate detection against"
        )

    devices = tuple(
        _device_summary(device_id, rows) for device_id, rows in sorted(by_device.items())
    )
    # A boot whose median gap is nowhere near the publish interval was not a
    # live 2-second capture -- a bulk replay or a backfill looks exactly like
    # this, and it must not be mistaken for device behaviour.
    for device in devices:
        for boot in device.boot_summaries:
            if boot.samples < 30:
                continue
            if abs(boot.median_cadence_seconds - CADENCE_SECONDS) > CADENCE_SECONDS / 2:
                findings.append(
                    f"{device.device_id}: boot {boot.boot_id} has a median gap of "
                    f"{boot.median_cadence_seconds:.2f}s against a {CADENCE_SECONDS:.0f}s "
                    "publish interval; this does not look like a live capture and must "
                    "not be approved without explaining it"
                )
    for device in devices:
        if device.usable_samples < 1000:
            findings.append(
                f"{device.device_id}: only {device.usable_samples} sample(s) survive the "
                f"30-sample contiguity rule across {device.usable_segments} usable "
                f"segment(s); training needs at least 1000 approved samples"
            )
        strides: Counter[str] = Counter()
        for boot in device.boot_summaries:
            for stride, count in boot.seq_stride_counts.items():
                strides[stride] += count
        dominant = strides.most_common(1)
        # The firmware's expected stride is documented in `preprocess`; a
        # *different* dominant stride means this capture did not come from the
        # configuration the pipeline is calibrated for.
        if dominant and dominant[0][0] not in ("1", str(EXPECTED_SEQ_STRIDE)):
            findings.append(
                f"{device.device_id}: the dominant seq stride between stored rows is "
                f"{dominant[0][0]}, and the documented firmware stride is "
                f"{EXPECTED_SEQ_STRIDE}. Either readings are missing or this capture ran "
                "under a different sampling configuration; an operator must establish "
                "which before this data can be called contiguous"
            )

    return Audit(
        database=str(Path(path)),
        generated_at=_iso(dt.datetime.now(dt.UTC)),
        total_samples=len(samples),
        total_devices=len(by_device),
        stored_anomalies=anomalies,
        calibration_fingerprint_available=fingerprint_available,
        provenance_recorded=provenance_recorded,
        devices=devices,
        blocking_findings=tuple(findings),
        notes=(
            "Counts describe the database. They are not evidence of data quality, "
            "and nothing here approves a row for training.",
        ),
    )


def render(report: Audit) -> str:
    """The audit as something an operator reads rather than parses."""
    lines: list[str] = [
        f"database: {report.database}",
        f"generated: {report.generated_at}",
        f"samples: {report.total_samples}   devices: {report.total_devices}   "
        f"stored anomalies: {report.stored_anomalies}",
        "calibration_fingerprint column: "
        + ("yes" if report.calibration_fingerprint_available else "NO"),
        f"provenance column: {'yes' if report.provenance_recorded else 'NO'}",
    ]
    for device in report.devices:
        lines.append("")
        lines.append(f"  device {device.device_id}")
        lines.append(f"    samples            {device.samples}")
        lines.append(f"    boots              {device.boots}")
        lines.append(
            f"    range              {device.first_received_at}"
            f" .. {device.last_received_at}"
        )
        lines.append(f"    sensor_status      {device.sensor_status_counts}")
        lines.append(f"    off-state samples  {device.off_state_samples}")
        lines.append(
            f"    segments           {device.contiguous_segments} "
            f"({device.usable_segments} usable, longest {device.longest_segment})"
        )
        lines.append(f"    usable samples     {device.usable_samples}")
        for boot in device.boot_summaries:
            lines.append(
                f"      boot {boot.boot_id}  n={boot.samples}  "
                f"cadence={boot.median_cadence_seconds:.2f}s  "
                f"seq_strides={boot.seq_stride_counts}  "
                f"off={boot.off_state_samples}"
            )
    if report.blocking_findings:
        lines.append("")
        lines.append("  BLOCKING FINDINGS")
        for finding in report.blocking_findings:
            lines.append(f"    - {finding}")
    lines.append("")
    for note in report.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    import argparse

    parser = argparse.ArgumentParser(
        prog="powerguard_ml.sqlite_source",
        description="Audit the backend SQLite database, or export approved rows.",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--json", type=Path, help="also write the audit as JSON")
    parser.add_argument(
        "--export",
        type=Path,
        help="write an approved .jsonl export (requires --manifest)",
    )
    parser.add_argument("--manifest", type=Path, help="operator review manifest")
    args = parser.parse_args(argv)

    report = audit(args.database)
    print(render(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")

    if args.export:
        if not args.manifest:
            print("\n--export requires --manifest: rows are exported only once an "
                  "operator has approved them", flush=True)
            return 2
        from powerguard_ml import dataset, review

        manifest = review.load_manifest(args.manifest)
        samples = read_telemetry(args.database, device_id=manifest.device_id)
        approved = review.apply_manifest(samples, manifest)
        args.export.parent.mkdir(parents=True, exist_ok=True)
        dataset.to_jsonl(approved, args.export)
        print(f"\napproved {len(approved)} of {len(samples)} row(s) -> {args.export}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
