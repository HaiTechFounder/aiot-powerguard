"""Fixed-microsecond UTC timestamp text."""

from __future__ import annotations

import datetime as dt

import pytest

from powerguard.db.types import TIMESTAMP_LENGTH, from_utc_text, to_utc_text


def test_round_trip_preserves_microseconds() -> None:
    moment = dt.datetime(2026, 9, 21, 1, 2, 3, 250000, tzinfo=dt.UTC)
    text = to_utc_text(moment)
    assert text == "2026-09-21T01:02:03.250000Z"
    assert len(text) == TIMESTAMP_LENGTH
    assert from_utc_text(text) == moment


def test_non_utc_input_is_normalised() -> None:
    tz = dt.timezone(dt.timedelta(hours=7))
    local = dt.datetime(2026, 9, 21, 8, 0, 0, tzinfo=tz)
    assert to_utc_text(local) == "2026-09-21T01:00:00.000000Z"


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_utc_text(dt.datetime(2026, 9, 21, 1, 0, 0))


def test_text_order_matches_chronological_order() -> None:
    base = dt.datetime(2026, 9, 21, 1, 0, 0, tzinfo=dt.UTC)
    stamps = [to_utc_text(base + dt.timedelta(microseconds=n)) for n in (0, 1, 999999, 1000000)]
    assert stamps == sorted(stamps)
