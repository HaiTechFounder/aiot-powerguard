"""A controllable stand-in for the Paho client.

This is deliberately *not* a stand-in for the adapter. It implements only
:class:`powerguard.mqtt.client.MqttTransport` — the handful of Paho calls the
adapter makes — so the production adapter code runs unchanged: its connect
handling, subscription checking, bounded queue, acknowledgement, saturation
path, redelivery request and shutdown ordering are all exercised for real.

Everything the adapter did is recorded in order, so a test can assert on
sequence ("drained before disconnect") and not merely on totals.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any

MQTT_ERR_SUCCESS = 0
MQTT_ERR_NO_CONN = 4
SUBACK_FAILURE = 0x80


@dataclass(slots=True)
class FakeReasonCode:
    """What Paho 2 hands to ``on_subscribe``: a granted QoS, or a failure."""

    value: int

    @property
    def is_failure(self) -> bool:
        return self.value == SUBACK_FAILURE


@dataclass(slots=True)
class FakeMessage:
    """The attributes the adapter reads off a Paho message."""

    topic: str
    payload: bytes
    mid: int
    qos: int = 1
    retain: bool = False


class FakeMqttTransport:
    def __init__(
        self,
        *,
        subscribe_result: int = MQTT_ERR_SUCCESS,
        connect_reason: object = 0,
    ) -> None:
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.on_subscribe: Any = None

        self.subscribe_result = subscribe_result
        self.connect_reason = connect_reason

        self.manual_ack: bool | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.connect_calls: list[tuple[str, int, int]] = []
        self.subscriptions: list[str] = []
        # mid -> topic filter, for the SUBACKs the test chooses to send.
        self.pending_subacks: dict[int, str] = {}
        self.unsubscriptions: list[str] = []
        self.acks: list[tuple[int, int]] = []
        self.reconnect_calls = 0
        self.reconnect_raises: Exception | None = None
        # Set to a cleared Event to make reconnect() block, exactly as the real
        # synchronous call does while a broker is slow to answer.
        self.reconnect_gate: threading.Event | None = None
        self.reconnect_entered = threading.Event()
        self.loop_started = 0
        self.loop_stopped = 0
        self.disconnects = 0
        # Ordered trace of every side effect, for ordering assertions.
        self.trace: list[str] = []
        self._mids = itertools.count(1)

    # -- MqttTransport -----------------------------------------------------

    def manual_ack_set(self, on: bool) -> None:
        self.manual_ack = on

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password)

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.connect_calls.append((host, port, keepalive))
        self.trace.append("connect")

    def loop_start(self) -> None:
        self.loop_started += 1
        self.trace.append("loop_start")

    def loop_stop(self) -> None:
        self.loop_stopped += 1
        self.trace.append("loop_stop")

    def disconnect(self) -> None:
        self.disconnects += 1
        self.trace.append("disconnect")

    def reconnect(self) -> int:
        self.reconnect_calls += 1
        self.trace.append("reconnect")
        self.reconnect_entered.set()
        if self.reconnect_gate is not None:
            # Blocks this (worker) thread, the way the real call blocks.
            self.reconnect_gate.wait(timeout=5)
        if self.reconnect_raises is not None:
            raise self.reconnect_raises
        return MQTT_ERR_SUCCESS

    def subscribe(self, topic: str, qos: int) -> tuple[int, int | None]:
        if self.subscribe_result != MQTT_ERR_SUCCESS:
            self.trace.append(f"subscribe_failed:{topic}")
            return self.subscribe_result, None
        assert qos == 1, "MQTT_SPEC requires QoS 1 subscriptions"
        self.subscriptions.append(topic)
        self.trace.append(f"subscribe:{topic}")
        mid = next(self._mids)
        # Queued locally. The broker has acknowledged nothing yet: only
        # `complete_subacks` or `send_suback` does that.
        self.pending_subacks[mid] = topic
        return MQTT_ERR_SUCCESS, mid

    def unsubscribe(self, topic: str) -> tuple[int, int | None]:
        self.unsubscriptions.append(topic)
        self.trace.append(f"unsubscribe:{topic}")
        return MQTT_ERR_SUCCESS, next(self._mids)

    def ack(self, mid: int, qos: int) -> None:
        self.acks.append((mid, qos))
        self.trace.append(f"ack:{mid}")

    # -- broker simulation -------------------------------------------------

    def complete_connect(self, reason: object | None = None) -> None:
        """Fire the CONNACK callback the way Paho's network thread would."""
        assert self.on_connect is not None
        self.on_connect(
            self, None, {}, self.connect_reason if reason is None else reason, None
        )

    def send_suback(self, mid: int, granted: int = 1) -> None:
        """Answer one SUBSCRIBE the way a broker would."""
        assert self.on_subscribe is not None
        self.pending_subacks.pop(mid, None)
        self.trace.append(f"suback:{mid}:{granted}")
        self.on_subscribe(self, None, mid, [FakeReasonCode(granted)], None)

    def complete_subacks(self, granted: int = 1) -> list[int]:
        """Answer every outstanding SUBSCRIBE. Returns the mids answered."""
        mids = list(self.pending_subacks)
        for mid in mids:
            self.send_suback(mid, granted)
        return mids

    def connect_and_subscribe(self, reason: object | None = None) -> None:
        """The whole happy path: CONNACK, then a SUBACK for every filter."""
        self.complete_connect(reason)
        self.complete_subacks()

    def drop_connection(self) -> None:
        assert self.on_disconnect is not None
        self.pending_subacks.clear()
        self.on_disconnect(self, None, 0)

    def deliver(
        self, topic: str, payload: bytes, *, mid: int | None = None, retain: bool = False
    ) -> int:
        """Hand one QoS 1 delivery to the adapter, as Paho would."""
        assert self.on_message is not None
        message_id = next(self._mids) if mid is None else mid
        self.on_message(
            self, None, FakeMessage(topic=topic, payload=payload, mid=message_id, retain=retain)
        )
        return message_id


@dataclass(slots=True)
class TransportRecorder:
    """Factory that hands the same transport to the adapter and to the test."""

    transport: FakeMqttTransport = field(default_factory=FakeMqttTransport)

    def __call__(self, _settings: object) -> FakeMqttTransport:
        return self.transport
