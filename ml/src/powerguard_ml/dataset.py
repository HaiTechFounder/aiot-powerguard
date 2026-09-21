"""The dataset schema, and reading it without trusting it.

A row is only usable if every field the pipeline reads is present and of the
right type, so parsing rejects rather than coerces: a malformed row names
itself and the line it came from instead of becoming a silent zero.

`received_at` is the authoritative order (ADR-006) and ids are assigned in that
order, exactly as the backend's own pagination assumes.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

SCHEMA_VERSION = "powerguard_dataset_v1"

#: Every column an export must carry. `label` is optional and operator-supplied.
REQUIRED_COLUMNS: tuple[str, ...] = (
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


class DatasetError(ValueError):
    """A dataset could not be read as the schema describes it."""


@dataclass(frozen=True, slots=True)
class Sample:
    id: int
    device_id: str
    boot_id: str
    seq: int
    received_at: dt.datetime
    voltage_v: float
    current_a: float
    power_w: float
    energy_wh: float
    sensor_status: str
    #: Operator-approved ground truth, when one exists. Never inferred.
    label: int | None = None
    #: Which electrical calibration produced the row; regimes never mix.
    calibration_fingerprint: str | None = None


def _require(row: dict[str, object], where: str) -> None:
    missing = [name for name in REQUIRED_COLUMNS if row.get(name) in (None, "")]
    if missing:
        raise DatasetError(f"{where}: missing required column(s): {', '.join(missing)}")


def _parse_time(value: object, where: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        stamp = value
    else:
        try:
            stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as failure:
            raise DatasetError(f"{where}: received_at is not RFC 3339: {value!r}") from failure
    if stamp.tzinfo is None:
        raise DatasetError(f"{where}: received_at must carry a UTC offset")
    return stamp.astimezone(dt.UTC)


def _as_int(value: object, name: str, where: str) -> int:
    """Coerce without guessing: a value that is not an integer is an error."""
    if isinstance(value, bool):
        raise DatasetError(f"{where}: {name} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (TypeError, ValueError) as failure:
            raise DatasetError(f"{where}: {name} is not an integer: {value!r}") from failure
    raise DatasetError(f"{where}: {name} is not an integer: {value!r}")


def _as_float(value: object, name: str, where: str) -> float:
    if isinstance(value, bool):
        raise DatasetError(f"{where}: {name} must be a number, not a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as failure:
            raise DatasetError(f"{where}: {name} is not a number: {value!r}") from failure
    raise DatasetError(f"{where}: {name} is not a number: {value!r}")


def _parse_label(value: object, where: str) -> int | None:
    if value is None or value == "":
        return None
    label = _as_int(value, "label", where)
    if label not in (0, 1):
        raise DatasetError(f"{where}: label must be 0 or 1, not {label}")
    return label


def parse_row(row: dict[str, object], where: str = "row") -> Sample:
    """One mapping to a `Sample`, or a `DatasetError` naming what was wrong."""
    _require(row, where)
    fingerprint = row.get("calibration_fingerprint")
    return Sample(
        id=_as_int(row["id"], "id", where),
        device_id=str(row["device_id"]),
        boot_id=str(row["boot_id"]),
        seq=_as_int(row["seq"], "seq", where),
        received_at=_parse_time(row["received_at"], where),
        voltage_v=_as_float(row["voltage_v"], "voltage_v", where),
        current_a=_as_float(row["current_a"], "current_a", where),
        power_w=_as_float(row["power_w"], "power_w", where),
        energy_wh=_as_float(row["energy_wh"], "energy_wh", where),
        sensor_status=str(row["sensor_status"]),
        label=_parse_label(row.get("label"), where),
        calibration_fingerprint=(
            str(fingerprint) if fingerprint is not None and fingerprint != "" else None
        ),
    )


def load_jsonl(path: Path | str) -> list[Sample]:
    rows: list[Sample] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as failure:
                raise DatasetError(f"line {number}: not JSON: {failure}") from failure
            if not isinstance(payload, dict):
                raise DatasetError(f"line {number}: expected a JSON object")
            rows.append(parse_row(payload, f"line {number}"))
    return rows


def load_csv(path: Path | str) -> list[Sample]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DatasetError("the CSV has no header row")
        absent = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
        if absent:
            raise DatasetError(f"the CSV header is missing: {', '.join(absent)}")
        return [
            parse_row(dict(row), f"line {number}")
            for number, row in enumerate(reader, start=2)
        ]


def load(path: Path | str) -> list[Sample]:
    """Read `.jsonl` or `.csv`, chosen by suffix."""
    suffix = Path(path).suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        return load_jsonl(path)
    if suffix == ".csv":
        return load_csv(path)
    raise DatasetError(f"unsupported dataset format: {suffix or '(none)'}")


def to_jsonl(samples: Iterable[Sample], path: Path | str) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(_as_row(sample)) + "\n")


def _as_row(sample: Sample) -> dict[str, object]:
    return {
        "id": sample.id,
        "device_id": sample.device_id,
        "boot_id": sample.boot_id,
        "seq": sample.seq,
        "received_at": sample.received_at.isoformat().replace("+00:00", "Z"),
        "voltage_v": sample.voltage_v,
        "current_a": sample.current_a,
        "power_w": sample.power_w,
        "energy_wh": sample.energy_wh,
        "sensor_status": sample.sensor_status,
        "label": sample.label,
        "calibration_fingerprint": sample.calibration_fingerprint,
    }


def relabel(samples: Sequence[Sample], labels: Iterable[int]) -> list[Sample]:
    """Attach labels positionally, for building fixtures."""
    return [replace(sample, label=label) for sample, label in zip(samples, labels, strict=True)]


def iter_devices(samples: Iterable[Sample]) -> Iterator[str]:
    seen: set[str] = set()
    for sample in samples:
        if sample.device_id not in seen:
            seen.add(sample.device_id)
            yield sample.device_id
