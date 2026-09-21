"""The schema, and what preparation is entitled to throw away."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from powerguard_ml.dataset import DatasetError, Sample, load, parse_row, to_jsonl
from powerguard_ml.preprocess import prepare, time_split
from powerguard_ml.synthetic import CALIBRATION_FINGERPRINT, generate

BASE_ROW = {
    "id": 1,
    "device_id": "pg-1",
    "boot_id": "boot-1",
    "seq": 1,
    "received_at": "2026-01-01T00:00:00Z",
    "voltage_v": 7.8,
    "current_a": 0.4,
    "power_w": 3.12,
    "energy_wh": 0.1,
    "sensor_status": "ok",
}


def test_parses_a_complete_row_into_utc() -> None:
    sample = parse_row(dict(BASE_ROW))
    assert sample.received_at.tzinfo is dt.UTC
    assert sample.label is None


@pytest.mark.parametrize("column", ["id", "device_id", "received_at", "power_w"])
def test_a_missing_required_column_names_itself(column: str) -> None:
    row = dict(BASE_ROW)
    del row[column]
    with pytest.raises(DatasetError, match=column):
        parse_row(row)


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc() -> None:
    row = dict(BASE_ROW) | {"received_at": "2026-01-01T00:00:00"}
    with pytest.raises(DatasetError, match="UTC offset"):
        parse_row(row)


def test_a_label_outside_zero_or_one_is_refused() -> None:
    with pytest.raises(DatasetError, match="0 or 1"):
        parse_row(dict(BASE_ROW) | {"label": 7})


def test_jsonl_round_trips(tmp_path) -> None:
    rows = generate(40)
    path = tmp_path / "data.jsonl"
    to_jsonl(rows, path)
    assert load(path) == rows


def test_an_unknown_suffix_is_refused(tmp_path) -> None:
    path = tmp_path / "data.parquet"
    path.write_text("", encoding="utf-8")
    with pytest.raises(DatasetError, match="unsupported"):
        load(path)


def test_rejected_readings_and_duplicates_never_reach_a_window() -> None:
    rows = generate(60)
    corrupted = [
        replace(rows[10], sensor_status="fault"),
        *rows,
        rows[20],  # the same id twice
    ]
    prepared = prepare(corrupted)
    assert prepared.dropped_sensor_status == 1
    assert prepared.dropped_duplicate == 1
    ids = [row.id for segment in prepared.segments for row in segment]
    assert len(ids) == len(set(ids))


def test_a_reboot_ends_a_segment() -> None:
    first = generate(35, boot_id="boot-a", start_id=1, start_seq=1)
    second = generate(
        35,
        boot_id="boot-b",
        start_id=100,
        start_seq=1,
        start_at=first[-1].received_at + dt.timedelta(seconds=2),
    )
    prepared = prepare(first + second)
    assert len(prepared.segments) == 2


def test_a_sequence_hole_ends_a_segment() -> None:
    rows = generate(70)
    holed = rows[:30] + rows[40:]
    prepared = prepare(holed)
    assert len(prepared.segments) == 2


def test_a_time_gap_over_twice_the_cadence_ends_a_segment() -> None:
    rows = list(generate(70))
    shifted = [
        replace(row, received_at=row.received_at + dt.timedelta(seconds=30))
        if index >= 35
        else row
        for index, row in enumerate(rows)
    ]
    assert len(prepare(shifted).segments) == 2


def test_a_segment_shorter_than_one_window_is_dropped_not_padded() -> None:
    prepared = prepare(generate(29))
    assert prepared.segments == ()
    assert prepared.dropped_short_segments == 1


def test_two_devices_in_one_dataset_are_refused() -> None:
    mixed = generate(35, device_id="pg-a") + generate(
        35, device_id="pg-b", start_id=500, boot_id="boot-b"
    )
    with pytest.raises(ValueError, match="one device"):
        prepare(mixed)


def test_two_calibration_regimes_are_refused() -> None:
    rows = generate(40)
    mixed = [*rows[:20], *[replace(row, calibration_fingerprint="other") for row in rows[20:]]]
    with pytest.raises(ValueError, match="one calibration regime"):
        prepare(mixed)


def test_the_split_never_lets_a_training_row_follow_a_calibration_row() -> None:
    rows = generate(200)
    split = time_split(rows)
    assert split.train and split.calibration
    assert max(row.received_at for row in split.train) < min(
        row.received_at for row in split.calibration
    )


def test_a_non_finite_reading_is_dropped() -> None:
    rows = list(generate(40))
    rows[5] = replace(rows[5], power_w=float("nan"))
    assert prepare(rows).dropped_non_finite == 1


def test_rows_out_of_order_are_sorted_by_received_at_then_id() -> None:
    rows = generate(40)
    prepared = prepare(list(reversed(rows)))
    ordered = [row for segment in prepared.segments for row in segment]
    assert ordered == sorted(ordered, key=lambda row: (row.received_at, row.id))


def test_a_sample_is_immutable() -> None:
    sample = parse_row(dict(BASE_ROW))
    with pytest.raises(AttributeError):
        sample.voltage_v = 0.0  # type: ignore[misc]
    assert isinstance(sample, Sample)


# -- incomplete calibration metadata ---------------------------------------


def test_a_named_regime_beside_unlabelled_rows_is_refused() -> None:
    # Which sensor calibration produced the unlabelled rows is unknowable, so
    # they may not be trained or scored alongside a named regime.
    rows = generate(40)
    mixed = [*rows[:20], *[replace(row, calibration_fingerprint=None) for row in rows[20:]]]
    with pytest.raises(ValueError, match="incomplete calibration metadata"):
        prepare(mixed)


def test_an_empty_fingerprint_counts_as_missing_not_as_a_regime() -> None:
    rows = generate(40)
    mixed = [*rows[:20], *[replace(row, calibration_fingerprint="") for row in rows[20:]]]
    with pytest.raises(ValueError, match="incomplete calibration metadata"):
        prepare(mixed)


def test_a_requested_regime_cannot_silently_discard_unlabelled_rows() -> None:
    # Passing the fingerprint must not become a way to filter the ambiguity
    # away: the mix is still refused.
    rows = generate(40)
    mixed = [*rows[:20], *[replace(row, calibration_fingerprint=None) for row in rows[20:]]]
    with pytest.raises(ValueError, match="incomplete calibration metadata"):
        prepare(mixed, calibration_fingerprint=CALIBRATION_FINGERPRINT)


def test_requesting_a_regime_a_dataset_does_not_declare_is_refused() -> None:
    rows = [replace(row, calibration_fingerprint=None) for row in generate(40)]
    with pytest.raises(ValueError, match="carries one"):
        prepare(rows, calibration_fingerprint=CALIBRATION_FINGERPRINT)


def test_requesting_the_wrong_regime_is_refused() -> None:
    with pytest.raises(ValueError, match="dataset carries"):
        prepare(generate(40), calibration_fingerprint="some-other-regime")


def test_a_dataset_with_no_fingerprint_anywhere_is_still_usable() -> None:
    # Declaring no regime at all is unambiguous; only a partial claim is not.
    rows = [replace(row, calibration_fingerprint=None) for row in generate(40)]
    assert prepare(rows).kept == 40


def test_a_complete_named_regime_passes() -> None:
    assert prepare(generate(40), calibration_fingerprint=CALIBRATION_FINGERPRINT).kept == 40
