"""Turning an export into something a window may legitimately span.

The feature contract requires 30 *contiguous* samples within one `boot_id`.
Contiguity is not a formality: a reboot resets the sequence, a dropped sample
leaves a hole, and a time gap means the rolling statistics would be computed
over an interval they were never meant to cover. Each of those ends a segment,
and a segment shorter than one window is dropped rather than padded.

The split is by time, never at random: training rows must precede calibration
rows or the score distribution is fitted on the future it is meant to judge.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from powerguard_ml.dataset import Sample

#: Firmware publishes every two seconds; twice that is the gap tolerance.
CADENCE_SECONDS = 2.0
GAP_TOLERANCE_FACTOR = 2.0


@dataclass(frozen=True, slots=True)
class PreparedData:
    """Segments that a window may span, and why rows were discarded."""

    segments: tuple[tuple[Sample, ...], ...]
    total_input: int
    dropped_sensor_status: int
    dropped_duplicate: int
    dropped_non_finite: int
    #: Segments discarded for being shorter than one window.
    dropped_short_segments: int

    @property
    def kept(self) -> int:
        return sum(len(segment) for segment in self.segments)


def _finite(sample: Sample) -> bool:
    values = (sample.voltage_v, sample.current_a, sample.power_w, sample.energy_wh)
    return all(value == value and value not in (float("inf"), float("-inf")) for value in values)


def prepare(
    samples: Iterable[Sample],
    *,
    window: int = 30,
    cadence_seconds: float = CADENCE_SECONDS,
    device_id: str | None = None,
    calibration_fingerprint: str | None = None,
) -> PreparedData:
    """Filter, order, dedupe and cut into contiguous segments.

    One device and one calibration regime only: an artifact trained across two
    electrical calibrations would be fitted on two different sensors.
    """
    rows = list(samples)
    total = len(rows)

    if device_id is not None:
        rows = [row for row in rows if row.device_id == device_id]

    devices = {row.device_id for row in rows}
    if len(devices) > 1:
        raise ValueError(
            f"one device per dataset; found {sorted(devices)}. Pass device_id to select one."
        )

    # The regime is checked *before* any fingerprint filter, so a dataset that
    # mixes labelled and unlabelled rows is refused rather than silently
    # reduced to whichever rows happened to carry a fingerprint.
    named = {row.calibration_fingerprint for row in rows if row.calibration_fingerprint}
    unnamed = sum(1 for row in rows if not row.calibration_fingerprint)
    if len(named) > 1:
        raise ValueError(
            f"one calibration regime per dataset; found {sorted(named)}. "
            "A new regime requires its own baseline and model."
        )
    if named and unnamed:
        # A named regime alongside rows that declare none is not a regime, it
        # is an assumption. Which sensor calibration produced those rows is
        # unknowable, so they may not be trained or scored together.
        raise ValueError(
            f"incomplete calibration metadata: {unnamed} row(s) carry no "
            f"calibration_fingerprint while others declare {sorted(named)[0]!r}. "
            "Every row of a named regime must carry its fingerprint."
        )
    if calibration_fingerprint is not None:
        if not named:
            raise ValueError(
                f"calibration_fingerprint {calibration_fingerprint!r} was requested but no row "
                "carries one; the dataset cannot be attributed to a calibration regime."
            )
        if calibration_fingerprint not in named:
            raise ValueError(
                f"calibration_fingerprint {calibration_fingerprint!r} was requested but the "
                f"dataset carries {sorted(named)[0]!r}."
            )
        rows = [row for row in rows if row.calibration_fingerprint == calibration_fingerprint]

    usable = [row for row in rows if row.sensor_status == "ok"]
    dropped_status = len(rows) - len(usable)

    finite = [row for row in usable if _finite(row)]
    dropped_non_finite = len(usable) - len(finite)

    # `received_at` first, id as the tie-break — the backend's own order.
    finite.sort(key=lambda row: (row.received_at, row.id))

    deduped: list[Sample] = []
    seen: set[int] = set()
    for row in finite:
        if row.id in seen:
            continue
        seen.add(row.id)
        deduped.append(row)
    dropped_duplicate = len(finite) - len(deduped)

    tolerance = cadence_seconds * GAP_TOLERANCE_FACTOR
    segments: list[list[Sample]] = []
    current: list[Sample] = []
    for row in deduped:
        if current:
            previous = current[-1]
            elapsed = (row.received_at - previous.received_at).total_seconds()
            contiguous = (
                row.boot_id == previous.boot_id
                and row.seq == previous.seq + 1
                and 0 < elapsed <= tolerance
            )
            if not contiguous:
                segments.append(current)
                current = []
        current.append(row)
    if current:
        segments.append(current)

    long_enough = [segment for segment in segments if len(segment) >= window]
    return PreparedData(
        segments=tuple(tuple(segment) for segment in long_enough),
        total_input=total,
        dropped_sensor_status=dropped_status,
        dropped_duplicate=dropped_duplicate,
        dropped_non_finite=dropped_non_finite,
        dropped_short_segments=len(segments) - len(long_enough),
    )


@dataclass(frozen=True, slots=True)
class TimeSplit:
    train: tuple[Sample, ...]
    calibration: tuple[Sample, ...]

    @property
    def boundary(self) -> str | None:
        return self.calibration[0].received_at.isoformat() if self.calibration else None


def time_split(rows: Sequence[Sample], *, train_fraction: float = 0.8) -> TimeSplit:
    """Split in time order. Every training row precedes every calibration row."""
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be strictly between 0 and 1")
    ordered = sorted(rows, key=lambda row: (row.received_at, row.id))
    cut = int(len(ordered) * train_fraction)
    return TimeSplit(train=tuple(ordered[:cut]), calibration=tuple(ordered[cut:]))
