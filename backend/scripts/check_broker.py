"""Check that a local broker accepts our credentials and our topics.

A replacement for checking a broker with `mosquitto_sub`, which takes the
password as a command-line argument and so puts it in the process argument list
where any other user on the machine can read it. Nothing here ever receives a
secret as an argument: the password is read from an environment variable and
handed straight to the client.

    $env:POWERGUARD_BROKER_PASSWORD = "<password>"
    python scripts/check_broker.py --username powerguard-backend

Exit codes: 0 reachable and authorised, 1 refused or unreachable, 2 misused.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

SUBSCRIPTIONS = (
    "powerguard/v1/devices/+/telemetry",
    "powerguard/v1/devices/+/status",
)
DEFAULT_PASSWORD_ENV = "POWERGUARD_BROKER_PASSWORD"


MINIMUM_GRANTED_QOS = 1
SUBACK_FAILURE = 0x80


def granted_qos(code: Any) -> int | None:
    """The QoS a SUBACK entry granted, or None when the filter was refused.

    A SUBACK entry is not a CONNACK reason: its numeric value *is* the granted
    QoS, and 0x80 — or a ``ReasonCode`` that says so — is the refusal.
    """
    if code is None:
        return None
    failed = getattr(code, "is_failure", None)
    if isinstance(failed, bool) and failed:
        return None
    value = getattr(code, "value", code)
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    return None if numeric not in (0, 1, 2) else numeric


@dataclass(slots=True)
class Result:
    """What the broker did, in terms that name no secret."""

    connected: bool = False
    reason: str = "no CONNACK received"
    granted: dict[str, int | None] = field(default_factory=dict)
    required: tuple[str, ...] = SUBSCRIPTIONS
    duplicates_ignored: int = 0
    unrelated_ignored: int = 0

    @property
    def missing(self) -> tuple[str, ...]:
        """Required filters the broker never answered for."""
        return tuple(topic for topic in self.required if topic not in self.granted)

    @property
    def ok(self) -> bool:
        """Every required filter confirmed, each at the QoS the design needs.

        One answer is not enough: a broker that acknowledges telemetry and
        silently drops status would otherwise look healthy.
        """
        if not self.connected or self.missing:
            return False
        return all(
            qos is not None and qos >= MINIMUM_GRANTED_QOS
            for qos in self.granted.values()
        )

    def lines(self) -> list[str]:
        out = [f"connect        = {'ok' if self.connected else 'refused'} ({self.reason})"]
        for topic in self.required:
            if topic not in self.granted:
                out.append(f"subscribe      = {topic} -> no SUBACK")
                continue
            qos = self.granted[topic]
            out.append(f"subscribe      = {topic} -> {'refused' if qos is None else qos}")
        if self.duplicates_ignored:
            out.append(f"duplicates     = {self.duplicates_ignored} ignored")
        if self.unrelated_ignored:
            out.append(f"unrelated      = {self.unrelated_ignored} ignored")
        return out


class SubscriptionTracker:
    """One-to-one ownership between a required filter and its SUBACK.

    Subscriptions are issued one at a time, so at most one ``subscribe()`` call
    is ever in flight. That is what makes the early-SUBACK race decidable: an
    answer whose message id is not yet known can only belong to the call that
    has not returned. Once that claim is made the id is remembered, so a
    duplicate of the same SUBACK is recognised as a duplicate rather than
    quietly satisfying the *other* filter.
    """

    def __init__(self, required: tuple[str, ...], result: Result) -> None:
        self._required = required
        self._result = result
        self._by_mid: dict[int, str] = {}
        self._inflight: str | None = None
        self._claimed_inflight = False
        self._seen_mids: set[int] = set()
        self.completions = 0

    @property
    def complete(self) -> bool:
        return not self._result.missing

    def begin(self, topic: str) -> None:
        """A subscribe call for `topic` is about to be issued."""
        self._inflight = topic
        self._claimed_inflight = False

    def settle(self, mid: int | None) -> None:
        """The subscribe call returned this message id."""
        if mid is not None and not self._claimed_inflight and self._inflight is not None:
            self._by_mid[mid] = self._inflight
        self._inflight = None
        self._claimed_inflight = False

    def record(self, mid: int, code: Any) -> None:
        """A SUBACK arrived. Returns nothing; the result is updated in place."""
        if mid in self._seen_mids:
            # The same answer twice must never fill another filter's slot.
            self._result.duplicates_ignored += 1
            return

        topic = self._by_mid.get(mid)
        if topic is None:
            if self._inflight is None or self._claimed_inflight:
                # Nothing is in flight, so this belongs to no request of ours.
                self._result.unrelated_ignored += 1
                return
            topic = self._inflight
            self._by_mid[mid] = topic
            self._claimed_inflight = True

        self._seen_mids.add(mid)
        if topic in self._result.granted:  # pragma: no cover - guarded by mids
            self._result.duplicates_ignored += 1
            return
        self._result.granted[topic] = granted_qos(code)
        if self.complete:
            self.completions += 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default="powerguard-backend")
    parser.add_argument(
        "--password-env",
        default=DEFAULT_PASSWORD_ENV,
        help="environment variable holding the password; never the password itself",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def check(
    host: str,
    port: int,
    username: str,
    password: str,
    timeout: float,
    client_factory: Any = None,
    required: tuple[str, ...] = SUBSCRIPTIONS,
) -> Result:
    """Connect, subscribe to every required filter, report what was granted.

    Success needs *all* of them confirmed at QoS 1 or better. A missing SUBACK
    is a timeout, not a pass.
    """
    result = Result(required=required)
    tracker = SubscriptionTracker(required, result)
    settled = threading.Event()

    def on_connect(
        client: Any, _userdata: Any, _flags: Any, reason: Any, _props: Any = None
    ) -> None:
        failed = getattr(reason, "is_failure", None)
        refused = (
            bool(failed) if isinstance(failed, bool) else str(reason) not in ("0", "Success")
        )
        result.connected = not refused
        result.reason = str(reason)
        if refused:
            settled.set()
            return
        for topic in required:
            tracker.begin(topic)
            _code, mid = client.subscribe(topic, MINIMUM_GRANTED_QOS)
            tracker.settle(mid)
        if tracker.complete:
            settled.set()

    def on_subscribe(
        _client: Any, _userdata: Any, mid: int, reason_codes: Any, _props: Any = None
    ) -> None:
        code = reason_codes[0] if isinstance(reason_codes, list | tuple) else reason_codes
        tracker.record(mid, code)
        if tracker.complete:
            settled.set()

    client = client_factory() if client_factory else _paho_client()
    # Credentials go straight into the client; they are never rendered anywhere.
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        if not settled.wait(timeout):
            missing = len(result.missing)
            result.reason = (
                f"no answer within {timeout}s"
                if not result.connected
                else f"{missing} of {len(required)} subscriptions unconfirmed after {timeout}s"
            )
    except Exception as exc:
        # Only the type: an exception message could quote what was sent.
        result.reason = f"{type(exc).__name__} while connecting"
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()
    return result


def _paho_client() -> Any:
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="powerguard-broker-check",
        protocol=mqtt.MQTTv311,
        reconnect_on_failure=False,
    )


def main(argv: list[str] | None = None, *, client_factory: Any = None) -> int:
    args = parse_args(argv)
    password = os.environ.get(args.password_env)
    if not password:
        print(f"set {args.password_env} with the broker password", file=sys.stderr)
        return 2

    result = check(
        args.host, args.port, args.username, password, args.timeout, client_factory
    )
    print(f"broker         = {args.host}:{args.port}")
    print(f"username       = {args.username}")
    for line in result.lines():
        print(line)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
