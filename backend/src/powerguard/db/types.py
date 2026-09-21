"""SQLAlchemy type adapters.

All timestamps are stored as fixed-microsecond UTC ISO-8601 text
(``YYYY-MM-DDTHH:MM:SS.ffffffZ``) so lexical ordering equals chronological
ordering in SQLite, and so equality comparisons are exact.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Dialect, String, TypeDecorator

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
TIMESTAMP_LENGTH = 27  # len("2026-09-21T01:02:03.250000Z")


def to_utc_text(value: dt.datetime) -> str:
    """Render an aware datetime as fixed-microsecond UTC text."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(dt.UTC).strftime(TIMESTAMP_FORMAT)


def from_utc_text(value: str) -> dt.datetime:
    """Parse fixed-microsecond UTC text back into an aware datetime."""
    return dt.datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=dt.UTC)


class UtcTimestamp(TypeDecorator[dt.datetime]):
    """Aware UTC datetime stored as fixed-microsecond ISO-8601 text."""

    impl = String(TIMESTAMP_LENGTH)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        return to_utc_text(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        return from_utc_text(str(value))
