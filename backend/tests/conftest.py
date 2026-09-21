"""Shared fixtures.

Every database used in tests is a temporary file migrated by Alembic — the same
migrations production runs. Nothing calls ``metadata.create_all()``.
"""

from __future__ import annotations

import datetime as dt
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from powerguard.config import Settings
from powerguard.db.engine import create_database_engine, create_session_factory
from powerguard.db.uow import SqlUnitOfWorkFactory
from powerguard.domain.services import FixedClock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
T0 = dt.datetime(2026, 9, 21, 3, 0, 0, tzinfo=dt.UTC)


def alembic_config(database_url: str, **x_args: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    # `-x key=value` arguments, the same channel the CLI uses.
    config.cmd_opts = Namespace(x=[f"{key}={value}" for key, value in x_args.items()])
    return config


def make_settings(database_url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": database_url,
        "mqtt_enabled": False,
        "app_env": "test",
    }
    base.update(overrides)
    # `_env_file=None` keeps the developer's own `.env` out of the test run.
    # The documented setup copies `.env.example` to `.env`, so without this a
    # developer who followed the README gets different settings — and different
    # test results — from CI or a clean checkout.
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'powerguard.db').as_posix()}"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return make_settings(database_url)


@pytest.fixture
def migrated_engine(settings: Settings) -> Iterator[Engine]:
    command.upgrade(alembic_config(settings.database_url), "head")
    engine = create_database_engine(settings)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def uow_factory(migrated_engine: Engine) -> SqlUnitOfWorkFactory:
    return SqlUnitOfWorkFactory(create_session_factory(migrated_engine))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)
