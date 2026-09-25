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


# -- the firmware's published sequence stride -------------------------------
#
# `PowerSensor::read` consumes a sequence number on every attempt and the
# firmware samples twice per publish, so a healthy ESP8266 stream advances
# `seq` by two per stored row. Treating that as a hole threw away almost every
# real reading; treating *any* advance as contiguous would hide real loss.


def test_the_documented_stride_stays_one_segment() -> None:
    rows = generate(70)
    strided = [replace(row, seq=row.seq * 2) for row in rows]
    prepared = prepare(strided)
    assert len(prepared.segments) == 1
    assert prepared.kept == 70


def test_publishing_every_sample_is_still_contiguous() -> None:
    # An advance below the stride means more data than expected, never less.
    assert len(prepare(generate(70)).segments) == 1


def test_a_missed_publish_still_ends_a_segment() -> None:
    """One skipped telemetry deadline is a genuinely missing reading."""
    rows = [replace(row, seq=row.seq * 2) for row in generate(70)]
    # Drop one published row: the next advance is 4, beyond the stride of 2.
    holed = rows[:35] + rows[36:]
    prepared = prepare(holed)
    assert len(prepared.segments) == 2


def test_a_stricter_stride_can_be_declared() -> None:
    # A device that publishes every sample declares 1, and an advance of 2 is
    # a hole again. The rule is a declaration, not a blanket relaxation.
    rows = [replace(row, seq=row.seq * 2) for row in generate(70)]
    assert len(prepare(rows, expected_seq_stride=1).segments) == 0


def test_a_reboot_still_ends_a_segment_whatever_the_stride() -> None:
    first = [replace(row, seq=row.seq * 2) for row in generate(35, boot_id="boot-a")]
    second = [
        replace(row, seq=row.seq * 2, boot_id="boot-b")
        for row in generate(
            35,
            boot_id="boot-b",
            start_id=100,
            start_at=first[-1].received_at + dt.timedelta(seconds=2),
        )
    ]
    assert len(prepare(first + second).segments) == 2


def test_a_time_gap_still_ends_a_segment_whatever_the_stride() -> None:
    rows = [replace(row, seq=row.seq * 2) for row in generate(70)]
    shifted = [
        replace(row, received_at=row.received_at + dt.timedelta(seconds=30))
        if index >= 35
        else row
        for index, row in enumerate(rows)
    ]
    assert len(prepare(shifted).segments) == 2


# -- one contiguity predicate, offline and online --------------------------


def test_the_shared_predicate_accepts_one_up_to_the_stride() -> None:
    from powerguard_ml.preprocess import is_contiguous

    first, second = generate(2)
    for advance, expected in ((0, False), (1, True), (2, True), (3, False), (-1, False)):
        candidate = replace(second, seq=first.seq + advance)
        assert is_contiguous(first, candidate, expected_seq_stride=2) is expected, advance


def test_the_shared_predicate_rejects_a_reboot_and_a_stall() -> None:
    from powerguard_ml.preprocess import is_contiguous

    first, second = generate(2)
    assert not is_contiguous(first, replace(second, boot_id="other"), expected_seq_stride=2)
    stalled = replace(second, received_at=first.received_at + dt.timedelta(seconds=5))
    assert not is_contiguous(first, stalled, expected_seq_stride=2)
    same_instant = replace(second, received_at=first.received_at)
    assert not is_contiguous(first, same_instant, expected_seq_stride=2)


@pytest.mark.parametrize("value", [0, -2, 9, "2", 2.0, True, None])
def test_an_unbounded_or_malformed_stride_is_refused(value) -> None:
    from powerguard_ml.preprocess import check_stride

    with pytest.raises(ValueError, match="expected_seq_stride"):
        check_stride(value)
    with pytest.raises(ValueError, match="expected_seq_stride"):
        prepare(generate(40), expected_seq_stride=value)  # type: ignore[arg-type]
