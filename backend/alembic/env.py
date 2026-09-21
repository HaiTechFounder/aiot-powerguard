"""Alembic environment.

Alembic is the only thing that creates schema, in production and in tests.
The URL comes from the application settings unless the caller overrides it via
``-x database_url=...`` or ``sqlalchemy.url``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from powerguard.config import Settings, parse_sqlite_url
from powerguard.db import models  # noqa: F401  (import registers the tables)
from powerguard.db.base import Base
from powerguard.db.engine import sqlite_pragmas

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would switch off every
    # `powerguard.*` logger already created in this process. Harmless when
    # `alembic upgrade` runs on its own, but it silently kills application
    # logging whenever migrations are run in-process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("database_url")
    if override:
        return str(override)
    configured = config.get_main_option("sqlalchemy.url", "")
    if configured:
        return configured
    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _default_busy_timeout_ms() -> int:
    return int(Settings.model_fields["sqlite_busy_timeout_ms"].default)


def _busy_timeout_ms() -> int:
    """The busy timeout this migration runs under.

    A migration must not require the whole application configuration to be
    valid — broker credentials have nothing to do with schema changes — so the
    value is resolved from the explicit ``-x`` argument, then the environment,
    and only then from full settings.
    """
    override = context.get_x_argument(as_dictionary=True).get("busy_timeout_ms")
    candidates = [override, os.environ.get("POWERGUARD_SQLITE_BUSY_TIMEOUT_MS")]
    for candidate in candidates:
        if candidate:
            try:
                return int(candidate)
            except ValueError:
                pass
    try:
        return Settings().sqlite_busy_timeout_ms
    except Exception:
        return _default_busy_timeout_ms()


def run_migrations_online() -> None:
    url = _database_url()
    # `create_database_engine` does this for the application, but migrations
    # build their own engine and normally run FIRST — on a clean checkout the
    # configured directory does not exist yet, and SQLite reports only
    # "unable to open database file" rather than the missing directory.
    sqlite_path = parse_sqlite_url(url)
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # The migration runs under exactly the same pragmas as the application
        # (foreign keys, WAL for file databases, a finite busy timeout), so a
        # concurrent reader cannot make `alembic upgrade` fail differently from
        # a normal write.
        for statement in sqlite_pragmas(_busy_timeout_ms(), parse_sqlite_url(url) is not None):
            connection.exec_driver_sql(statement)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLAlchemy 2 autobegins a transaction and SQLite runs DDL in
        # "non-transactional" mode, so alembic's own block does not commit the
        # version row. Without this the schema would be created but the
        # revision never recorded, and every upgrade would run again.
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
