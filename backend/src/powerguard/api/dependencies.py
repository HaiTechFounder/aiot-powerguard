"""Request-scoped access to the composition root and query parsing helpers."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastapi import Depends, Query, Request

from powerguard.api.errors import InvalidQueryError
from powerguard.api.schemas import DEFAULT_LIMIT, MAX_LIMIT, MIN_LIMIT
from powerguard.mqtt.topics import is_valid_device_id

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from powerguard.bootstrap import Container

T = TypeVar("T")


def get_container(request: Request) -> Container:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only if lifespan did not run
        raise RuntimeError("application container is not initialised")
    return container


ContainerDep = Annotated["Container", Depends(get_container)]


async def run_in_db_thread(func: Any, *args: Any) -> Any:
    """Run blocking SQLAlchemy work off the event loop."""
    return await asyncio.to_thread(func, *args)


def parse_device_id(device_id: str) -> str:
    if not is_valid_device_id(device_id):
        raise InvalidQueryError("device_id must match ^[a-z0-9][a-z0-9_-]{0,31}$")
    return device_id


def _parse_instant(value: str | None, field: str) -> dt.datetime | None:
    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidQueryError(f"{field} must be RFC 3339 UTC") from exc
    if parsed.tzinfo is None:
        raise InvalidQueryError(f"{field} must include a UTC offset")
    if parsed.utcoffset() != dt.timedelta(0):
        raise InvalidQueryError(f"{field} must be UTC")
    return parsed.astimezone(dt.UTC)


class HistoryQuery:
    """Validated, bounded history window."""

    def __init__(
        self,
        since: Annotated[str | None, Query(alias="from")] = None,
        until: Annotated[str | None, Query(alias="to")] = None,
        limit: Annotated[int, Query(ge=MIN_LIMIT, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> None:
        self.since = _parse_instant(since, "from")
        self.until = _parse_instant(until, "to")
        self.limit = limit
        self.before_id = before_id
        # `from` is inclusive and `to` exclusive, so an empty or inverted
        # window is a client error rather than a silently empty page.
        if self.since is not None and self.until is not None and self.until <= self.since:
            raise InvalidQueryError("to must be later than from")


HistoryQueryDep = Annotated[HistoryQuery, Depends()]
DeviceIdDep = Annotated[str, Depends(parse_device_id)]
