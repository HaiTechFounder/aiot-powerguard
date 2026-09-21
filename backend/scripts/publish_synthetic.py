"""Publish synthetic PowerGuard v1 telemetry.

Stands in for the firmware while no hardware exists. Valid payloads are
byte-for-byte what the device emits and stay inside the hardware contract
(2S pack, 8.4 V ceiling), so ingestion is exercised through the real contract
rather than a convenient approximation.

    # print what would be sent, no broker needed
    python scripts/publish_synthetic.py --dry-run --count 5

    # every scenario, reproducibly
    python scripts/publish_synthetic.py --dry-run --scenario all --seed 7

    # publish to a local broker
    python scripts/publish_synthetic.py --host 127.0.0.1 --username u --password-env PW

Two things are kept strictly apart:

**Plan generation** is pure. Given the same arguments and the same ``--seed`` it
produces an identical run — sequence numbers, measurements, boot ids, topics,
scenario order and payload bytes included. It reads no clock, no UUID and no
global random state, so a plan can be compared byte for byte.

**Live execution** is the only part that touches a broker or a clock. It walks
the plan, waits for each QoS 1 publication to be confirmed, and performs the one
action a payload cannot express: dropping the connection so the broker publishes
the session's Last Will.

A run is a sequence of *sessions*. Each session has its own ``boot_id``, its own
registered will, and owns every message published while it holds the connection.
A drop ends a session; the reconnect that follows establishes a new one, with a
new boot id and a new will. Nothing from a finished session — least of all its
boot id — is reused afterwards. Paho's own automatic reconnect is switched off,
so the only thing that re-establishes a connection is this program.

The password is only ever read from an environment variable, never from the
command line, so it cannot reach shell history, a process listing or this
program's output. Every message is published to the v1 topics of the configured
device and nowhere else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TELEMETRY_TOPIC = "powerguard/v1/devices/{device_id}/telemetry"
STATUS_TOPIC = "powerguard/v1/devices/{device_id}/status"

# MQTT_SPEC device id grammar. Anything else would build a topic outside the
# device's own namespace.
DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Hardware contract: 2S 18650, 8.4 V ceiling. Generated valid samples stay
# under it, jitter included, so a demo reading is never rejected as out of
# range by the backend it is meant to demonstrate.
PACK_CEILING_V = 8.4
NOMINAL_V = 8.39
MAX_MESSAGES = 10_000  # a bounded run, whatever the flags say

# `sampled_at`, when asked for, is a *logical* timestamp derived from this
# epoch and the sample index. A wall clock would make a plan unreproducible,
# and MQTT_SPEC only requires a UTC RFC 3339 value with milliseconds.
LOGICAL_EPOCH = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.UTC)

# How long a graceful offline publication may take to be confirmed.
PUBLISH_TIMEOUT_S = 5.0

PUBLISH = "publish"
DROP = "drop"

# `Client.loop_start()` returns MQTT_ERR_INVAL when a network thread already
# exists, and `loop_stop()` joins the previous one. Both matter at a session
# handoff, so both results are checked rather than assumed.
MQTT_ERR_SUCCESS = 0

SCENARIOS: dict[str, str] = {
    "normal": "steady discharge, sequence increasing by one",
    "duplicate": "re-sends one sample unchanged, to exercise QoS 1 idempotency",
    "spike": "a brief current/power surge, still inside the hardware contract",
    "drift": "voltage sagging steadily across the run",
    "gap": "skips sequence numbers, so the backend counts a gap",
    "out-of-order": "delivers a later sequence before an earlier one",
    "malformed": "payloads the backend must reject and acknowledge",
    "status": "online, telemetry, then a graceful offline",
    "reconnect": "an unexpected drop so the broker publishes the will, then a reboot",
}


@dataclass(frozen=True, slots=True)
class Message:
    """One step of a plan.

    ``action`` is ``publish`` for everything the device sends. ``drop`` is the
    one step a payload cannot express: the device disappears without a
    DISCONNECT, and the **broker** publishes the will it registered for that
    session. ``body`` then records what the broker is expected to publish, so a
    dry run still shows the whole story.
    """

    topic: str
    body: str
    retain: bool = False
    label: str = ""
    action: str = PUBLISH


@dataclass(frozen=True, slots=True)
class Session:
    """One simulated device session: one connection, one boot id, one will.

    Everything published while this session holds the connection carries its
    ``boot_id``, as MQTT_SPEC requires — its telemetry, its status messages,
    the will the broker holds for it, and the graceful offline that ends it.
    """

    boot_id: str
    will_body: str
    messages: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def ends_with_drop(self) -> bool:
        return bool(self.messages) and self.messages[-1].action == DROP


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete, reproducible run: one or more sessions, in order."""

    device_id: str
    firmware: str
    sessions: tuple[Session, ...] = field(default_factory=tuple)

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(m for session in self.sessions for m in session.messages)

    @property
    def boot_id(self) -> str:
        """The boot id the run starts in."""
        return self.sessions[0].boot_id

    @property
    def will_body(self) -> str:
        """The will registered when the run connects."""
        return self.sessions[0].will_body

    @property
    def final_session(self) -> Session:
        """The session that owns the connection when the run ends."""
        return self.sessions[-1]

    @property
    def boot_ids(self) -> tuple[str, ...]:
        return tuple(session.boot_id for session in self.sessions)


def boot_id(rng: random.Random) -> str:
    """Eight lowercase hex characters, drawn from the seeded generator."""
    return f"{rng.getrandbits(32):08x}"


def status_payload(boot: str, status: str, firmware: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "boot_id": boot,
        "firmware_version": firmware,
    }


def logical_timestamp(index: int, interval: float) -> str:
    """A deterministic RFC 3339 UTC timestamp with milliseconds."""
    moment = LOGICAL_EPOCH + dt.timedelta(seconds=index * interval)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def telemetry_payload(
    boot: str,
    seq: int,
    energy_wh: float,
    firmware: str,
    *,
    sampled_at: str | None = None,
    voltage: float | None = None,
    current: float | None = None,
    rng: random.Random,
) -> dict[str, Any]:
    """One valid v1 telemetry document.

    ``voltage``/``current`` let a scenario shape the reading; both are clamped
    to the hardware contract so no scenario can emit an impossible sample.
    """
    if voltage is None:
        # A plausible 2S 18650 discharge: slow sag plus a small ripple.
        voltage = NOMINAL_V - 0.4 * math.tanh(seq / 60.0) + rng.uniform(-0.01, 0.01)
    if current is None:
        current = 0.35 + 0.05 * math.sin(seq / 7.0) + rng.uniform(-0.005, 0.005)
    voltage = min(voltage, PACK_CEILING_V)
    power = voltage * current
    return {
        "schema_version": 1,
        "boot_id": boot,
        "seq": seq,
        "sampled_at": sampled_at,
        "voltage_v": round(voltage, 3),
        "current_a": round(current, 3),
        "power_w": round(power, 3),
        "energy_wh": round(max(energy_wh, 0.0), 6),
        "sensor_status": "ok",
        "firmware_version": firmware,
    }


def malformed_bodies(
    boot: str, seq: int, firmware: str, rng: random.Random
) -> list[tuple[str, str]]:
    """Payloads the backend must reject deterministically, with their reason."""
    valid = telemetry_payload(boot, seq, 0.5, firmware, voltage=7.8, current=0.4, rng=rng)
    unknown = dict(valid, extra_field="nope")
    wrong_version = dict(valid, schema_version=2)
    out_of_range = dict(valid, voltage_v=99.0, power_w=99.0)
    bad_boot = dict(valid, boot_id="ZZZ")
    return [
        ("{not json", "json_invalid"),
        (json.dumps(unknown), "schema_invalid: unknown field"),
        (json.dumps(wrong_version), "version_unsupported"),
        (json.dumps(out_of_range), "measurement_out_of_range"),
        (json.dumps(bad_boot), "schema_invalid: boot_id"),
    ]


def _telemetry_message(device_id: str, payload: dict[str, Any], label: str) -> Message:
    return Message(
        topic=TELEMETRY_TOPIC.format(device_id=device_id),
        body=json.dumps(payload),
        label=label,
    )


def _status_message(
    device_id: str, boot: str, status: str, firmware: str, label: str
) -> Message:
    return Message(
        topic=STATUS_TOPIC.format(device_id=device_id),
        body=json.dumps(status_payload(boot, status, firmware)),
        retain=True,
        label=label,
    )


def _drop_message(device_id: str, boot: str, firmware: str) -> Message:
    """The device vanishes; the broker publishes the will for this session."""
    return Message(
        topic=STATUS_TOPIC.format(device_id=device_id),
        body=json.dumps(status_payload(boot, "offline", firmware)),
        retain=True,
        label="unexpected drop (the broker publishes the will)",
        action=DROP,
    )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """A scenario's messages, and the session identity it leaves behind.

    ``boot`` is what a following scenario must continue with: a scenario that
    reboots hands back the *new* boot id, never the one it started with.
    """

    messages: tuple[Message, ...]
    boot: str


def build_scenario(
    scenario: str,
    *,
    device_id: str,
    firmware: str,
    boot: str,
    count: int,
    start_seq: int,
    with_timestamp: bool = False,
    interval: float = 2.0,
    rng: random.Random,
) -> ScenarioResult:
    """The exact messages a scenario would produce. Pure; no broker, no clock.

    Building the whole run up front is what lets the tests assert on payloads
    and topics without a broker, keeps every run bounded by construction, and
    makes a seeded run reproducible byte for byte.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    messages: list[Message] = []
    energy = 0.0
    seq = start_seq
    emitted = 0

    def sample(**kwargs: Any) -> dict[str, Any]:
        nonlocal energy, emitted
        stamp = logical_timestamp(emitted, interval) if with_timestamp else None
        payload = telemetry_payload(
            boot, seq, energy, firmware, sampled_at=stamp, rng=rng, **kwargs
        )
        energy += payload["power_w"] / 1800.0
        emitted += 1
        return payload

    if scenario == "normal":
        for _ in range(count):
            messages.append(_telemetry_message(device_id, sample(), "normal"))
            seq += 1

    elif scenario == "duplicate":
        for index in range(count):
            payload = sample()
            messages.append(_telemetry_message(device_id, payload, "normal"))
            if index % 2 == 1:
                # Same (device_id, boot_id, seq): one row, acknowledged twice.
                messages.append(_telemetry_message(device_id, payload, "duplicate"))
            seq += 1

    elif scenario == "spike":
        for index in range(count):
            spiking = index == count // 2
            payload = sample(current=3.2 if spiking else None)
            messages.append(
                _telemetry_message(device_id, payload, "spike" if spiking else "normal")
            )
            seq += 1

    elif scenario == "drift":
        for index in range(count):
            # A steady sag towards the low end of the pack, not a cliff.
            voltage = NOMINAL_V - (1.6 * index / max(count - 1, 1))
            messages.append(_telemetry_message(device_id, sample(voltage=voltage), "drift"))
            seq += 1

    elif scenario == "gap":
        for index in range(count):
            messages.append(_telemetry_message(device_id, sample(), "normal"))
            # Skip ahead halfway through: counted, never backfilled.
            seq += 5 if index == count // 2 else 1

    elif scenario == "out-of-order":
        for index in range(count):
            if index % 3 == 2:
                # Build seq+1 first, then seq, and publish them in that order.
                seq += 1
                later = sample()
                seq -= 1
                earlier = sample()
                seq += 2  # resume after the pair
                messages.append(_telemetry_message(device_id, later, "out-of-order (later)"))
                messages.append(
                    _telemetry_message(device_id, earlier, "out-of-order (earlier)")
                )
            else:
                messages.append(_telemetry_message(device_id, sample(), "normal"))
                seq += 1

    elif scenario == "malformed":
        for body, reason in malformed_bodies(boot, seq, firmware, rng)[:count]:
            messages.append(
                Message(
                    topic=TELEMETRY_TOPIC.format(device_id=device_id),
                    body=body,
                    label=f"malformed ({reason})",
                )
            )

    elif scenario == "status":
        # A clean session: announce, report, then say goodbye properly. The
        # offline here is published by the device and confirmed before it
        # disconnects — it is not a will.
        messages.append(_status_message(device_id, boot, "online", firmware, "status online"))
        for _ in range(count):
            messages.append(_telemetry_message(device_id, sample(), "normal"))
            seq += 1
        messages.append(
            _status_message(device_id, boot, "offline", firmware, "graceful offline")
        )

    elif scenario == "reconnect":
        half = max(count // 2, 1)
        messages.append(_status_message(device_id, boot, "online", firmware, "status online"))
        for _ in range(half):
            messages.append(_telemetry_message(device_id, sample(), "normal"))
            seq += 1
        # The device disappears. Nothing publishes an offline payload here: the
        # broker does it, from the will registered for this same boot id.
        messages.append(_drop_message(device_id, boot, firmware))
        # A restart: a new boot id, drawn from the same seeded generator, and
        # the sequence starts again from 1.
        boot = boot_id(rng)
        energy = 0.0
        seq = 1
        messages.append(_status_message(device_id, boot, "online", firmware, "reboot online"))
        for _ in range(count - half):
            messages.append(_telemetry_message(device_id, sample(), "after reboot"))
            seq += 1

    return ScenarioResult(tuple(messages[:MAX_MESSAGES]), boot)


def build_messages(scenario: str, **kwargs: Any) -> list[Message]:
    """The messages of one scenario. See :func:`build_scenario`."""
    return list(build_scenario(scenario, **kwargs).messages)


def split_sessions(messages: list[Message], first_boot: str, firmware: str) -> list[Session]:
    """Cut a flat run into sessions at every drop.

    A drop is the last message of the session it kills. Whatever follows
    belongs to the replacement session, whose boot id is taken from the first
    message that carries one — and whose will is built from that, so the will
    always describes the session that actually holds the connection.
    """
    sessions: list[Session] = []
    current: list[Message] = []
    boot = first_boot

    def close(messages_: list[Message], boot_: str) -> None:
        sessions.append(
            Session(
                boot_id=boot_,
                will_body=json.dumps(status_payload(boot_, "offline", firmware)),
                messages=tuple(messages_),
            )
        )

    for message in messages:
        current.append(message)
        if message.action == DROP:
            close(current, boot)
            current = []
            boot = ""  # decided by the first message of the next session
        elif not boot:
            boot = _boot_of(message) or ""
    if current or not sessions:
        close(current, boot or first_boot)
    return sessions


def _boot_of(message: Message) -> str | None:
    try:
        document = json.loads(message.body)
    except json.JSONDecodeError:
        return None
    value = document.get("boot_id")
    return str(value) if isinstance(value, str) else None


def _scenario_help() -> str:
    lines = [f"  {name:<14} {text}" for name, text in SCENARIOS.items()]
    return "scenarios:\n" + "\n".join(lines) + "\n  all            every scenario in order"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_scenario_help(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        default="normal",
        help="one of the scenarios listed below, a comma-separated list, or 'all'",
    )
    parser.add_argument(
        "--list-scenarios", action="store_true", help="print the scenarios and exit"
    )
    parser.add_argument("--device-id", default="powerguard-01")
    parser.add_argument("--firmware", default="0.1.0")
    parser.add_argument("--count", type=int, default=10, help="samples per scenario")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between messages")
    parser.add_argument("--start-seq", type=int, default=1)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="reproduce a run exactly; the same seed gives the same plan",
    )
    parser.add_argument(
        "--with-timestamp",
        action="store_true",
        help="send sampled_at as a logical UTC timestamp; the firmware sends null",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default=None)
    parser.add_argument(
        "--password-env",
        default="POWERGUARD_DEVICE_PASSWORD",
        help="environment variable holding the broker password; never the password itself",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, publish nothing")
    return parser.parse_args(argv)


def resolve_scenarios(value: str) -> list[str]:
    if value == "all":
        return list(SCENARIOS)
    names = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenario(s): {', '.join(unknown)}")
    return names


def plan(args: argparse.Namespace) -> Plan:
    """The complete run this invocation would perform. Pure and reproducible."""
    if not DEVICE_ID_PATTERN.match(args.device_id):
        raise ValueError(f"device id must match {DEVICE_ID_PATTERN.pattern}")
    if args.count < 1:
        raise ValueError("count must be at least 1")

    rng = random.Random(args.seed)
    first_boot = boot_id(rng)
    boot = first_boot
    messages: list[Message] = []
    for scenario in resolve_scenarios(args.scenario):
        result = build_scenario(
            scenario,
            device_id=args.device_id,
            firmware=args.firmware,
            boot=boot,
            count=args.count,
            start_seq=args.start_seq,
            with_timestamp=args.with_timestamp,
            interval=args.interval,
            rng=rng,
        )
        messages.extend(result.messages)
        # A scenario that rebooted hands back the new identity; the next
        # scenario continues in that session, not in the one that died.
        boot = result.boot
    messages = messages[:MAX_MESSAGES]

    allowed = (
        TELEMETRY_TOPIC.format(device_id=args.device_id),
        STATUS_TOPIC.format(device_id=args.device_id),
    )
    for message in messages:
        # A scenario can only ever address this device's own v1 topics.
        assert message.topic in allowed, f"refusing to publish to {message.topic}"

    return Plan(
        device_id=args.device_id,
        firmware=args.firmware,
        sessions=tuple(split_sessions(messages, first_boot, args.firmware)),
    )


def render(plan_: Plan) -> list[str]:
    """The dry-run rendering of a plan. Deterministic, like the plan itself."""
    lines: list[str] = []
    for index, session in enumerate(plan_.sessions, start=1):
        lines.append(
            f"# session {index} boot_id={session.boot_id} device={plan_.device_id}"
        )
        lines.append(f"# will (registered at connect) {session.will_body}")
        for message in session.messages:
            retained = " [retained]" if message.retain else ""
            prefix = "DROP " if message.action == DROP else ""
            lines.append(
                f"{prefix}{message.topic}{retained} {message.body}  # {message.label}"
            )
    lines.append(
        f"# graceful offline (at exit) {plan_.final_session.will_body}"
    )
    return lines


class PublishNotConfirmedError(RuntimeError):
    """A QoS 1 publication was not confirmed within the timeout."""


class LoopHandoffError(RuntimeError):
    """The replacement session could not get a network loop of its own."""


def publish_confirmed(client: Any, message: Message, timeout: float) -> None:
    """Publish at QoS 1 and wait for the broker to confirm it.

    Returning before the PUBACK arrives would make a graceful offline a lie:
    the process could exit with the announcement still in a local buffer.
    """
    info = client.publish(message.topic, message.body, qos=1, retain=message.retain)
    info.wait_for_publish(timeout=timeout)
    if not info.is_published():
        raise PublishNotConfirmedError(f"{message.topic} was not confirmed within {timeout}s")


def drop_connection(client: Any) -> None:
    """Vanish without a DISCONNECT, so the broker publishes the will.

    Closing the socket is the only honest way to do this: publishing an offline
    payload by hand would prove nothing about the will the broker holds.
    """
    client._sock_close()


def start_loop(client: Any) -> None:
    """Start exactly one network loop, and believe the answer.

    Paho refuses with ``MQTT_ERR_INVAL`` when a thread already exists. Ignoring
    that would leave the client with no loop at all while the program carried
    on publishing into a buffer nothing drains.
    """
    result = client.loop_start()
    if result != MQTT_ERR_SUCCESS:
        raise LoopHandoffError(f"loop_start refused with {result}")


def handoff(client: Any, will_topic: str, will_body: str) -> None:
    """Retire the dead session's network loop, then open the replacement.

    ``loop_stop()`` joins the previous thread, so the old loop has certainly
    exited before a new one is asked for — two loops on one client would
    interleave reads on the same socket. Only then is the next session's will
    registered and the connection re-established.
    """
    client.loop_stop()
    client.will_set(will_topic, will_body, qos=1, retain=True)
    client.reconnect()
    start_loop(client)


def build_client(args: argparse.Namespace, will_topic: str, will_body: str) -> Any:
    """The Paho client this publisher drives.

    ``reconnect_on_failure=False`` is the supported switch in paho-mqtt 2.x. It
    matters here: with Paho retrying on its own, its reconnect would race the
    explicit one in :func:`execute`, and a drop meant to be deliberate could be
    silently undone by the library. This program is the only thing that
    re-establishes a connection.

    The password never reaches an argument list: it is read from the
    environment and handed straight to the client.
    """
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"powerguard-device-{args.device_id}",
        protocol=mqtt.MQTTv311,
        reconnect_on_failure=False,
    )
    if args.username:
        client.username_pw_set(args.username, os.environ.get(args.password_env))
    client.will_set(will_topic, will_body, qos=1, retain=True)
    return client


def execute(
    run: Plan,
    client: Any,
    *,
    interval: float,
    timeout: float = PUBLISH_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    out: Any = None,
) -> int:
    """Walk the plan against a connected client. Returns the exit code.

    Session boundaries are what this function exists for. A drop ends the
    session that owns the connection; before reconnecting, the *next* session's
    will is registered, so the broker never holds a will describing a session
    that has already died.
    """
    stream = out if out is not None else sys.stdout
    status = STATUS_TOPIC.format(device_id=run.device_id)
    current = run.sessions[0]
    connected = True

    try:
        for index, session in enumerate(run.sessions):
            current = session
            remaining = len(session.messages)
            for message in session.messages:
                remaining -= 1
                if message.action == DROP:
                    print(f"drop {message.topic} # {message.label}", file=stream)
                    drop_connection(client)
                    connected = False
                    # The replacement session: a new identity, and a will that
                    # describes it, registered before the connection is made.
                    following = run.sessions[index + 1]
                    sleep(interval)
                    handoff(client, status, following.will_body)
                    connected = True
                    continue
                publish_confirmed(client, message, timeout)
                print(f"sent {message.topic} # {message.label}", file=stream)
                if remaining:
                    sleep(interval)
    except PublishNotConfirmedError as exc:
        print(f"publish not confirmed: {exc}", file=sys.stderr)
        _shutdown(client, connected)
        return 1
    except LoopHandoffError as exc:
        # The reconnect did not really succeed; say so rather than carrying on.
        print(f"reconnect failed: {exc}", file=sys.stderr)
        _shutdown(client, connected=False)
        return 1
    except KeyboardInterrupt:
        pass

    if not connected:  # pragma: no cover - a drop is always followed by a session
        client.loop_stop()
        return 0

    # A graceful goodbye, in the identity that actually holds the connection:
    # announce offline, wait for the broker to confirm it, and only then leave.
    offline = Message(status, current.will_body, retain=True, label="graceful offline")
    try:
        publish_confirmed(client, offline, timeout)
    except PublishNotConfirmedError as exc:
        # Never claim a goodbye that the broker did not acknowledge.
        print(f"graceful offline not confirmed: {exc}", file=sys.stderr)
        _shutdown(client, connected=True)
        return 1
    print(f"sent {offline.topic} # {offline.label}", file=stream)
    _shutdown(client, connected=True)
    return 0


def _shutdown(client: Any, connected: bool) -> None:
    """Leave no network thread behind, whatever ended the run."""
    if connected:
        client.disconnect()
    # loop_stop() joins, so the thread has exited by the time this returns.
    client.loop_stop()


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[argparse.Namespace, str, str], Any] | None = None,
) -> int:
    args = parse_args(argv)
    if args.list_scenarios:
        print(_scenario_help())
        return 0

    try:
        run = plan(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.dry_run:
        for line in render(run):
            print(line)
        return 0

    if args.username and not os.environ.get(args.password_env):
        print(
            f"set {args.password_env} with the broker password, or use --dry-run",
            file=sys.stderr,
        )
        return 2

    factory = client_factory or build_client
    status = STATUS_TOPIC.format(device_id=run.device_id)
    client = factory(args, status, run.will_body)
    client.connect(args.host, args.port, keepalive=30)
    try:
        start_loop(client)
    except LoopHandoffError as exc:
        print(f"could not start the network loop: {exc}", file=sys.stderr)
        return 1
    return execute(run, client, interval=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
