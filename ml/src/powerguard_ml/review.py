"""The operator review manifest: the only thing that can approve a row.

The backend's database records what arrived, not whether it is fit to train
on. Two facts the pipeline depends on are simply not in it -- which
calibration regime produced a reading, and whether a reading came from
hardware at all -- and neither can be inferred from the numbers. So they are
attested, in a file a person writes and signs, and every approved row carries
that attestation forward into the artifact metadata.

The manifest is allow-list shaped on purpose. A row is excluded unless an
interval explicitly covers it, so forgetting to exclude the off-state hour at
the start of a capture cannot silently put it in the training set.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from powerguard_ml.dataset import Sample
from powerguard_ml.preprocess import EXPECTED_SEQ_STRIDE, check_stride

MANIFEST_VERSION = "powerguard_review_v1"

#: Only an operator's word puts a capture in this class, and only this class
#: may train a model that is allowed to be promoted.
PROVENANCE_HARDWARE = "hardware"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_UNKNOWN = "unknown"
PROVENANCE_VALUES = (PROVENANCE_HARDWARE, PROVENANCE_SYNTHETIC, PROVENANCE_UNKNOWN)


class ReviewError(ValueError):
    """A manifest is absent, malformed, or claims something it may not."""


@dataclass(frozen=True, slots=True)
class Interval:
    """A half-open window of approved rows: ``[from, to)``."""

    start: dt.datetime
    end: dt.datetime
    label: int
    description: str = ""

    def covers(self, moment: dt.datetime) -> bool:
        return self.start <= moment < self.end


@dataclass(frozen=True, slots=True)
class Manifest:
    """One witnessed capture, described by the person who witnessed it.

    Everything here is an attestation. None of it can be recovered from the
    database afterwards, which is precisely why it has to be written down
    while the capture is being made.
    """

    device_id: str
    calibration_fingerprint: str
    provenance: str
    reviewed_by: str
    reviewed_at: str
    #: Normal operating data the operator approves for training (label 0).
    approved_intervals: tuple[Interval, ...]
    #: The firmware build that produced the capture. A different build may
    #: sample or publish differently, which changes what contiguity means.
    firmware_version: str = ""
    #: The shunt the current reading was derived through. Changing it changes
    #: the electrical calibration, so it is part of the regime's identity.
    shunt_resistance_ohm: float = 0.0
    #: How far `seq` advances per published row under this firmware build.
    expected_seq_stride: int = EXPECTED_SEQ_STRIDE
    #: Power cycles the operator witnessed. When non-empty, rows from any
    #: other boot are not approved, however well they fit an interval.
    boot_ids: tuple[str, ...] = ()
    #: Abnormal events the operator witnessed and labelled (label 1).
    labelled_events: tuple[Interval, ...] = ()
    notes: str = ""
    manifest_version: str = MANIFEST_VERSION

    @property
    def is_hardware(self) -> bool:
        return self.provenance == PROVENANCE_HARDWARE

    @property
    def intervals(self) -> tuple[Interval, ...]:
        return self.approved_intervals + self.labelled_events


def _parse_time(value: object, where: str) -> dt.datetime:
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as failure:
        raise ReviewError(f"{where}: not an RFC 3339 timestamp: {value!r}") from failure
    if stamp.tzinfo is None:
        raise ReviewError(f"{where}: timestamp must carry a UTC offset")
    return stamp.astimezone(dt.UTC)


def _parse_interval(payload: object, where: str, *, label: int) -> Interval:
    if not isinstance(payload, dict):
        raise ReviewError(f"{where}: expected an object")
    for key in ("from", "to"):
        if key not in payload:
            raise ReviewError(f"{where}: missing {key!r}")
    start = _parse_time(payload["from"], f"{where}.from")
    end = _parse_time(payload["to"], f"{where}.to")
    if end <= start:
        raise ReviewError(f"{where}: 'to' must be after 'from'")
    declared = payload.get("label", label)
    if declared not in (0, 1):
        raise ReviewError(f"{where}: label must be 0 or 1, not {declared!r}")
    return Interval(
        start=start,
        end=end,
        label=int(declared),
        description=str(payload.get("description", "")),
    )


def parse_manifest(payload: dict[str, Any]) -> Manifest:
    version = payload.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ReviewError(f"manifest_version {version!r} is not {MANIFEST_VERSION!r}")

    required = (
        "device_id",
        "calibration_fingerprint",
        "provenance",
        "reviewed_by",
        "reviewed_at",
        "firmware_version",
    )
    missing = [name for name in required if not str(payload.get(name, "")).strip()]
    if missing:
        raise ReviewError(f"manifest is missing: {', '.join(missing)}")

    try:
        shunt = float(payload.get("shunt_resistance_ohm", 0.0))
    except (TypeError, ValueError) as failure:
        raise ReviewError("shunt_resistance_ohm is not a number") from failure
    if shunt <= 0.0:
        raise ReviewError(
            "shunt_resistance_ohm must be the positive shunt value this capture ran "
            "through; it is part of the calibration regime's identity"
        )

    # Bounded, and never coerced: "2.7" or true is a review error, not a 2.
    try:
        stride = check_stride(payload.get("expected_seq_stride", EXPECTED_SEQ_STRIDE))
    except ValueError as failure:
        raise ReviewError(str(failure)) from failure

    boots_raw = payload.get("boot_ids", [])
    if not isinstance(boots_raw, list):
        raise ReviewError("boot_ids must be a list")
    boot_ids = tuple(str(value) for value in boots_raw if str(value).strip())

    provenance = str(payload["provenance"])
    if provenance not in PROVENANCE_VALUES:
        raise ReviewError(
            f"provenance {provenance!r} must be one of {', '.join(PROVENANCE_VALUES)}"
        )

    approved_raw = payload.get("approved_intervals", [])
    events_raw = payload.get("labelled_events", [])
    if not isinstance(approved_raw, list) or not isinstance(events_raw, list):
        raise ReviewError("approved_intervals and labelled_events must be lists")
    if not approved_raw:
        raise ReviewError(
            "approved_intervals is empty: a manifest that approves nothing approves nothing"
        )

    approved = tuple(
        _parse_interval(item, f"approved_intervals[{index}]", label=0)
        for index, item in enumerate(approved_raw)
    )
    for index, interval in enumerate(approved):
        if interval.label != 0:
            raise ReviewError(
                f"approved_intervals[{index}]: normal training data must carry label 0; "
                "an abnormal stretch belongs in labelled_events"
            )
    events = tuple(
        _parse_interval(item, f"labelled_events[{index}]", label=1)
        for index, item in enumerate(events_raw)
    )

    # Overlap between a "normal" claim and an "abnormal" claim is a review
    # error, and silently preferring one would bury it.
    for approved_index, normal in enumerate(approved):
        for event_index, event in enumerate(events):
            if normal.start < event.end and event.start < normal.end:
                raise ReviewError(
                    f"approved_intervals[{approved_index}] overlaps "
                    f"labelled_events[{event_index}]: a stretch cannot be both normal "
                    "and an abnormal event"
                )

    return Manifest(
        device_id=str(payload["device_id"]),
        calibration_fingerprint=str(payload["calibration_fingerprint"]),
        provenance=provenance,
        reviewed_by=str(payload["reviewed_by"]),
        reviewed_at=str(payload["reviewed_at"]),
        approved_intervals=approved,
        firmware_version=str(payload["firmware_version"]),
        shunt_resistance_ohm=shunt,
        expected_seq_stride=stride,
        boot_ids=boot_ids,
        labelled_events=events,
        notes=str(payload.get("notes", "")),
    )


def load_manifest(path: Path | str) -> Manifest:
    target = Path(path)
    if not target.exists():
        raise ReviewError(f"no review manifest at {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as failure:
        raise ReviewError(f"{target}: manifest is not JSON: {failure}") from failure
    if not isinstance(payload, dict):
        raise ReviewError(f"{target}: manifest is not an object")
    return parse_manifest(payload)


def apply_manifest(samples: Iterable[Sample], manifest: Manifest) -> list[Sample]:
    """Keep only approved rows, stamped with the attested regime and label.

    A row outside every interval is dropped: approval is granted, never
    assumed. A row from another device is dropped too -- one manifest attests
    to one device.
    """
    approved: list[Sample] = []
    for sample in samples:
        if sample.device_id != manifest.device_id:
            continue
        # A witnessed capture is witnessed per power cycle. A boot the operator
        # did not list is not approved, whatever interval it falls inside.
        if manifest.boot_ids and sample.boot_id not in manifest.boot_ids:
            continue
        match = next(
            (interval for interval in manifest.intervals if interval.covers(sample.received_at)),
            None,
        )
        if match is None:
            continue
        approved.append(
            replace(
                sample,
                label=match.label,
                calibration_fingerprint=manifest.calibration_fingerprint,
            )
        )
    return approved


def template(device_id: str, samples: Sequence[Sample]) -> dict[str, Any]:
    """A starting point for a review, with the intervals left for the operator.

    It deliberately produces a manifest that will **not** pass the gate:
    provenance is ``unknown`` and the fingerprint is a placeholder, so somebody
    has to look at the capture and say what it was before anything trains on it.
    """
    first = samples[0].received_at if samples else dt.datetime.now(dt.UTC)
    last = samples[-1].received_at if samples else first
    if last <= first:
        # A zero-width interval is not a valid manifest, and a template that
        # cannot be parsed is not a usable starting point.
        last = first + dt.timedelta(hours=1)
    return {
        "manifest_version": MANIFEST_VERSION,
        "device_id": device_id,
        "calibration_fingerprint": "REPLACE-ME-with-the-regime-this-capture-ran-under",
        "provenance": PROVENANCE_UNKNOWN,
        "firmware_version": "REPLACE-ME-with-the-build-that-produced-this-capture",
        "shunt_resistance_ohm": 0.01,
        "expected_seq_stride": EXPECTED_SEQ_STRIDE,
        "boot_ids": sorted({sample.boot_id for sample in samples}),
        "reviewed_by": "REPLACE-ME",
        "reviewed_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "notes": (
            "Approve only stretches you witnessed: one calibration regime, load on, "
            "no off-state idling, no bench experiments. Delete this note when done."
        ),
        "approved_intervals": [
            {
                "from": first.isoformat().replace("+00:00", "Z"),
                "to": last.isoformat().replace("+00:00", "Z"),
                "label": 0,
                "description": "REPLACE-ME: narrow this to what you actually approve",
            }
        ],
        "labelled_events": [],
    }
