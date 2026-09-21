"""Command line entry point: ``python -m powerguard <command>``.

Commands are deliberately thin. ``--help`` must work without a database, a
broker or a ``.env``, so nothing here touches configuration at import time.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from powerguard import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="powerguard", description="AIoT PowerGuard backend")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP API and MQTT ingestion")
    sub.add_parser("check-config", help="validate configuration and print a redacted summary")
    sub.add_parser("check-db", help="report the SQLite mode and current migration revision")
    return parser


def _cmd_check_config() -> int:
    from powerguard.config import Settings

    try:
        settings = Settings()
    except Exception as exc:
        print(f"configuration invalid: {exc}", file=sys.stderr)
        return 2
    for key, value in sorted(settings.redacted().items()):
        print(f"{key} = {value}")
    return 0


def _cmd_check_db() -> int:
    from powerguard.bootstrap import current_revision
    from powerguard.config import Settings
    from powerguard.db.engine import create_database_engine, journal_mode, pragma_value

    settings = Settings()
    engine = create_database_engine(settings)
    try:
        print(f"database_url   = {settings.database_url}")
        print(f"journal_mode   = {journal_mode(engine)}")
        print(f"foreign_keys   = {pragma_value(engine, 'foreign_keys')}")
        print(f"busy_timeout   = {pragma_value(engine, 'busy_timeout')}")
        print(f"alembic_head   = {current_revision(engine) or '<none>'}")
    finally:
        engine.dispose()
    return 0


def _cmd_serve() -> int:
    import uvicorn

    from powerguard.config import Settings
    from powerguard.main import create_app
    from powerguard.observability import configure_logging, secrets_from_settings

    settings = Settings()
    configure_logging(
        settings.log_level, settings.log_format, secrets=secrets_from_settings(settings)
    )
    uvicorn.run(
        create_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
        # Liveness is ASGI ping/pong; the configured values must actually reach
        # the server or the setting would be decorative.
        ws_ping_interval=settings.ws_ping_interval_s,
        ws_ping_timeout=settings.ws_ping_timeout_s,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check-config":
        return _cmd_check_config()
    if args.command == "check-db":
        return _cmd_check_db()
    if args.command == "serve":
        return _cmd_serve()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
