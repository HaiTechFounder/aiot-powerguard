"""Clock adapter used by the domain."""

from __future__ import annotations

import datetime as dt


class SystemClock:
    """Authoritative server time: timezone-aware UTC (ADR-006)."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, moment: dt.datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("fixed clock needs an aware datetime")
        self._moment = moment

    def now(self) -> dt.datetime:
        return self._moment

    def advance(self, seconds: float) -> None:
        self._moment += dt.timedelta(seconds=seconds)
