"""SQLAlchemy engine construction and per-connection SQLite controls.

Every connection gets ``foreign_keys=ON``, a finite ``busy_timeout`` and — for
file databases — WAL journalling. In-memory databases cannot use WAL; the mode
that SQLite actually reports is returned rather than assumed, so tests assert
the real state instead of a pretended one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from powerguard.config import Settings


def sqlite_pragmas(busy_timeout_ms: int, use_wal: bool) -> tuple[str, ...]:
    """The one authoritative pragma list.

    Runtime connections, Alembic connections and tests all use this, so a
    migration can never run under different durability or locking rules than
    the application that follows it.
    """
    statements = [
        "PRAGMA foreign_keys=ON",
        f"PRAGMA busy_timeout={int(busy_timeout_ms)}",
    ]
    if use_wal:
        # WAL is impossible for an in-memory database; SQLite silently keeps
        # `memory` journalling, so it is only requested for file databases.
        statements.append("PRAGMA journal_mode=WAL")
    statements.append("PRAGMA synchronous=NORMAL")
    return tuple(statements)


def apply_sqlite_pragmas(dbapi_connection: Any, busy_timeout_ms: int, use_wal: bool) -> None:
    """Apply :func:`sqlite_pragmas` to a raw DB-API connection."""
    cursor = dbapi_connection.cursor()
    try:
        for statement in sqlite_pragmas(busy_timeout_ms, use_wal):
            cursor.execute(statement)
    finally:
        cursor.close()


def create_database_engine(settings: Settings) -> Engine:
    """Build the engine for this process and install the connection hooks."""
    sqlite_path = settings.sqlite_path
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    use_wal = sqlite_path is not None
    engine = create_engine(
        settings.database_url,
        future=True,
        # Synchronous SQLAlchemy work runs in worker threads; a session is
        # created and closed entirely inside the thread that uses it.
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        if isinstance(dbapi_connection, sqlite3.Connection):
            apply_sqlite_pragmas(dbapi_connection, settings.sqlite_busy_timeout_ms, use_wal)

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def journal_mode(engine: Engine) -> str:
    """Journal mode SQLite reports for this engine, lowercased."""
    with engine.connect() as connection:
        return str(connection.execute(text("PRAGMA journal_mode")).scalar_one()).lower()


def pragma_value(engine: Engine, pragma: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(f"PRAGMA {pragma}")).scalar_one()


@contextmanager
def disposable_engine(settings: Settings) -> Iterator[Engine]:
    engine = create_database_engine(settings)
    try:
        yield engine
    finally:
        engine.dispose()


def database_file(settings: Settings) -> Path | None:
    return settings.sqlite_path
