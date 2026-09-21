"""The one path a device status transition travels.

A device can change state from three places: a status message from the device,
telemetry proving it is alive again, and the stale scan deciding it has gone
quiet. Before this existed, only the first of those emitted an event, so a
``stale -> online`` recovery — the transition an operator most wants to see —
was invisible.

Every one of them now calls :meth:`DeviceStatusTracker.record`, which is the
only place the transition event is emitted and the only place the counter moves.
It reports whether the state actually changed, so a repeated announcement
produces neither an event nor a count, and callers can use the answer to decide
whether a broadcast is worth sending.

The tracker is process-local diagnostics over the database, not a second source
of truth: the row in ``devices`` remains authoritative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from powerguard.domain.entities import DeviceStatus
from powerguard.observability import log_event

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"


@dataclass(slots=True)
class StatusCounters:
    """The device status category required by BACKEND_SPEC section 13."""

    status_transitions: int = 0
    transitions_by_status: dict[str, int] = field(default_factory=dict)


class DeviceStatusTracker:
    def __init__(self) -> None:
        self.counters = StatusCounters()
        self._current: dict[str, DeviceStatus] = {}

    def current(self, device_id: str) -> DeviceStatus | None:
        """Last status this process observed, or None if it has seen none."""
        return self._current.get(device_id)

    def record(
        self,
        device_id: str,
        status: DeviceStatus,
        *,
        source: str,
        boot_id: str | None = None,
        retained: bool = False,
    ) -> bool:
        """Note a device's state. True when it actually changed.

        ``source`` names the origin — ``status_message``, ``telemetry`` or
        ``stale_scan`` — so a transition can be traced back to what caused it.
        """
        previous = self._current.get(device_id)
        if previous == status:
            return False

        self._current[device_id] = status
        self.counters.status_transitions += 1
        self.counters.transitions_by_status[status.value] = (
            self.counters.transitions_by_status.get(status.value, 0) + 1
        )
        log_event(
            logger,
            logging.INFO,
            "device_status_transition",
            device_id=device_id,
            previous=previous.value if previous is not None else UNKNOWN,
            status=status.value,
            source=source,
            boot_id=boot_id,
            retained=retained,
        )
        return True

    def forget(self, device_id: str) -> None:
        """Drop remembered state, so the next record reports it as new."""
        self._current.pop(device_id, None)
