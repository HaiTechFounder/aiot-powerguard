"""Typed domain and application errors."""

from __future__ import annotations


class PowerGuardError(Exception):
    """Base class for every error this application raises deliberately."""


class TransientStorageError(PowerGuardError):
    """Storage failed in a way that may succeed later.

    Ingestion must not acknowledge the MQTT delivery when this is raised.
    """


class DeviceNotFoundError(PowerGuardError):
    def __init__(self, device_id: str) -> None:
        super().__init__(f"unknown device: {device_id}")
        self.device_id = device_id


class InferenceUnavailableError(PowerGuardError):
    """No usable model. Telemetry still flows; only the verdict is skipped."""
