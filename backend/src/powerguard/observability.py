"""Structured logging and redaction.

Every operational record is an *event*: a stable snake_case name plus a small
set of non-secret correlation fields (device id, boot id, sequence, row id,
message id, topic). Payload bytes, credentials and whole settings objects are
never logged.

Two independent guards, because one is not enough:

* :func:`log_event` redacts by field name and refuses to render raw bytes, so a
  careless call site cannot leak a payload;
* :class:`SecretScrubber` is installed on the root logger and removes the
  literal secret values from any record, including ones produced by third-party
  libraries that this project does not control.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

# Field names whose value is never printed, whatever it contains.
_SENSITIVE_MARKERS = ("password", "passwd", "secret", "token", "credential", "authorization")
# Field names that carry message content rather than correlation data.
_CONTENT_FIELDS = frozenset({"payload", "body", "message_payload", "document"})

REDACTED = "<redacted>"


def redact_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Replace secret-looking values and summarise byte content by length."""
    clean: dict[str, object] = {}
    for key, value in fields.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            clean[key] = REDACTED
        elif lowered in _CONTENT_FIELDS:
            if isinstance(value, bytes | bytearray):
                clean[key] = f"<{len(value)} bytes>"
            else:
                clean[key] = REDACTED
        elif isinstance(value, bytes | bytearray):
            # Content never reaches a log line, only its size.
            clean[key] = f"<{len(value)} bytes>"
        else:
            clean[key] = value
    return clean


def _render(event: str, fields: Mapping[str, object]) -> str:
    parts = [event]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    return " ".join(parts)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Emit one structured event record."""
    clean = redact_fields(fields)
    logger.log(level, _render(event, clean), extra={"event": event, "fields": clean})


class SecretScrubber(logging.Filter):
    """Removes literal secret values from every record that passes through.

    Used twice: as a filter, so the message a handler sees is already clean,
    and inside the formatters, because a traceback is rendered *after* filters
    run and would otherwise carry a secret from an exception message straight
    to the output.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Longest first, so an overlapping secret cannot leave a fragment.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - broken %-args in a foreign record
            return True
        scrubbed = self.scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        return True


class _Scrubbing:
    """Formatter mixin: nothing leaves without a final pass over the secrets."""

    _scrubber: SecretScrubber


class JsonFormatter(_Scrubbing, logging.Formatter):
    """One JSON object per line, with a UTC ISO-8601 timestamp."""

    def __init__(self, scrubber: SecretScrubber | None = None) -> None:
        super().__init__()
        self._scrubber = scrubber or SecretScrubber(())

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage().split(" ", 1)[0]),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload["fields"] = fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return self._scrubber.scrub(json.dumps(payload, default=str, sort_keys=True))


class ConsoleFormatter(_Scrubbing, logging.Formatter):
    """Human-readable single line; the timestamp is UTC, never local time."""

    def __init__(self, fmt: str, scrubber: SecretScrubber | None = None) -> None:
        super().__init__(fmt)
        self._scrubber = scrubber or SecretScrubber(())

    def format(self, record: logging.LogRecord) -> str:
        return self._scrubber.scrub(super().format(record))

    def formatTime(  # noqa: N802  (logging.Formatter hook name)
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        moment = dt.datetime.fromtimestamp(record.created, dt.UTC)
        if datefmt:
            return moment.strftime(datefmt)
        return moment.isoformat(timespec="milliseconds")


_INSTALLED_MARKER = "_powerguard_handler"


def configure_logging(
    log_level: str = "INFO", log_format: str = "console", *, secrets: Iterable[str] = ()
) -> None:
    """Install the process log configuration. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _INSTALLED_MARKER, False):
            root.removeHandler(handler)

    scrubber = SecretScrubber(secrets)
    handler = logging.StreamHandler()
    setattr(handler, _INSTALLED_MARKER, True)
    if log_format == "json":
        handler.setFormatter(JsonFormatter(scrubber))
    else:
        handler.setFormatter(
            ConsoleFormatter("%(asctime)s %(levelname)-7s %(name)s %(message)s", scrubber)
        )
    # The scrubber sits on the handler, not on a logger: a logger filter only
    # sees records logged through that logger, while a handler filter sees
    # every record that reaches output, including ones from libraries this
    # project does not control. The formatter repeats the pass so a secret
    # inside a rendered traceback is caught too.
    handler.addFilter(scrubber)
    root.addHandler(handler)
    root.setLevel(log_level.upper())


def secrets_from_settings(settings: object) -> tuple[str, ...]:
    """Literal secret values to scrub, taken from the settings object."""
    collected: list[str] = []
    password = getattr(settings, "mqtt_password", None)
    if password is not None:
        value = getattr(password, "get_secret_value", None)
        if callable(value):
            collected.append(str(value()))
    return tuple(s for s in collected if s)
