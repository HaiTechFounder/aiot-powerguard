"""The typed errors the outcome matrix depends on.

`TransientStorageError` is the only thing that makes ingestion withhold an
acknowledgement, so the hierarchy that lets a caller catch it is worth pinning
down rather than assuming.
"""

from __future__ import annotations

import datetime as dt

import pytest

from powerguard.domain.errors import (
    DeviceNotFoundError,
    InferenceUnavailableError,
    PowerGuardError,
    TransientStorageError,
)
from powerguard.domain.services import FixedClock, SystemClock


@pytest.mark.parametrize(
    "error",
    [TransientStorageError, DeviceNotFoundError, InferenceUnavailableError],
)
def test_every_deliberate_error_shares_one_base(error: type[Exception]) -> None:
    assert issubclass(error, PowerGuardError)
    assert issubclass(error, Exception)


def test_a_missing_device_names_itself_without_leaking_anything_else() -> None:
    error = DeviceNotFoundError("powerguard-01")

    assert error.device_id == "powerguard-01"
    assert str(error) == "unknown device: powerguard-01"


def test_a_transient_error_is_not_mistaken_for_a_missing_device() -> None:
    """Catching one must never catch the other: they mean opposite things."""
    with pytest.raises(TransientStorageError):
        raise TransientStorageError("database is locked")

    assert not issubclass(TransientStorageError, DeviceNotFoundError)
    assert not issubclass(DeviceNotFoundError, TransientStorageError)


def test_a_fixed_clock_refuses_a_naive_datetime() -> None:
    """Every timestamp in this system is aware UTC; a test clock is no exception."""
    with pytest.raises(ValueError, match="aware datetime"):
        FixedClock(dt.datetime(2026, 9, 21, 3, 0))


def test_a_fixed_clock_does_not_move() -> None:
    moment = dt.datetime(2026, 9, 21, 3, 0, tzinfo=dt.UTC)
    clock = FixedClock(moment)

    assert clock.now() == moment
    assert clock.now() == moment


def test_the_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)
