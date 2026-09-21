"""Injected fakes for the mandatory no-broker verification path.

``FakeMqttClient`` records exactly which deliveries were acknowledged and when,
so the outcome matrix in BACKEND_SPEC section 9 is observable without Mosquitto.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from powerguard.domain.entities import Anomaly, AnomalyVerdict, Device, Telemetry
from powerguard.mqtt.ports import InboundMessage
from powerguard.mqtt.topics import status_topic, telemetry_topic


class FakeMqttClient:
    """Stands in for the Paho adapter."""

    def __init__(self) -> None:
        self.acknowledged: list[int] = []
        self.reconnect_reasons: list[str] = []
        self._mids = itertools.count(1)

    def acknowledge(self, message: InboundMessage) -> None:
        self.acknowledged.append(message.mid)

    def request_reconnect(self, reason: str) -> None:
        self.reconnect_reasons.append(reason)

    # -- helpers for tests -------------------------------------------------

    def telemetry_message(
        self, device_id: str, payload: bytes, received_at: dt.datetime, *, retained: bool = False
    ) -> InboundMessage:
        return InboundMessage(
            topic=telemetry_topic(device_id),
            payload=payload,
            received_at=received_at,
            mid=next(self._mids),
            retained=retained,
        )

    def status_message(
        self, device_id: str, payload: bytes, received_at: dt.datetime, *, retained: bool = False
    ) -> InboundMessage:
        return InboundMessage(
            topic=status_topic(device_id),
            payload=payload,
            received_at=received_at,
            mid=next(self._mids),
            retained=retained,
        )

    def raw_message(
        self, topic: str, payload: bytes, received_at: dt.datetime
    ) -> InboundMessage:
        return InboundMessage(
            topic=topic, payload=payload, received_at=received_at, mid=next(self._mids)
        )


@dataclass
class RecordingPublisher:
    """Captures the broadcast order so telemetry-before-anomaly is provable."""

    events: list[tuple[str, object]] = field(default_factory=list)

    async def publish_telemetry(self, telemetry: Telemetry) -> None:
        self.events.append(("telemetry", telemetry))

    async def publish_anomaly(self, anomaly: Anomaly) -> None:
        self.events.append(("anomaly", anomaly))

    async def publish_device_status(self, device: Device) -> None:
        self.events.append(("status", device))

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


class FakeInference:
    """Returns a scripted verdict, or raises, without a real model."""

    def __init__(
        self, verdict: AnomalyVerdict | None = None, *, failure: Exception | None = None
    ) -> None:
        self._verdict = verdict
        self._failure = failure
        self.calls = 0

    def readiness(self) -> str:
        return "ready" if self._verdict is not None else "unavailable"

    def evaluate(self, telemetry: Telemetry) -> AnomalyVerdict | None:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        del telemetry
        return self._verdict
