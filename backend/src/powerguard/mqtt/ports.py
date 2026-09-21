"""Ports for the MQTT adapter.

The ingestion service never talks to Paho directly: it consumes
:class:`InboundMessage` values and reports an acknowledgement decision back
through :class:`MqttClientPort`. That keeps acknowledgement timing observable
in tests without a broker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One delivery, captured at the edge of the backend.

    ``received_at`` is taken in the network callback, as close to arrival as
    possible: queue delay, database time and ``sampled_at`` never replace it
    (ADR-006).
    """

    topic: str
    payload: bytes
    received_at: dt.datetime
    mid: int
    qos: int = 1
    retained: bool = False


class AckDecision(StrEnum):
    """Whether this delivery may be acknowledged to the broker."""

    ACK = "ack"
    # Withheld so the broker redelivers: the message was never durably handled.
    WITHHOLD = "withhold"


class MqttClientPort(Protocol):
    def acknowledge(self, message: InboundMessage) -> None:
        """Manually acknowledge a QoS 1 delivery."""
        ...

    def request_reconnect(self, reason: str) -> None:
        """Drop and re-establish the session so unacknowledged work is resent."""
        ...
