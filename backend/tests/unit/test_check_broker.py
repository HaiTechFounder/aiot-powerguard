"""The broker connectivity check, and the secret it must never reveal.

It replaced `mosquitto_sub` with a password flag. The point of the replacement
is that the password exists only in the environment and in the client, so that
is what these tests hold it to.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_broker.py"
SECRET = "do-not-print-me"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_broker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_broker = load()


class FakeReasonCode:
    def __init__(self, value: int, is_failure: bool = False) -> None:
        self.value = value
        self.is_failure = is_failure


class FakeBrokerClient:
    """Answers connect and subscribe the way a broker would."""

    def __init__(self, *, connack: Any = 0, granted: int | None = 1) -> None:
        self.on_connect: Any = None
        self.on_subscribe: Any = None
        self.credentials: tuple[str, str] | None = None
        self.calls: list[str] = []
        self._connack = connack
        self._granted = granted
        self._mid = 0

    def username_pw_set(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def connect(self, host: str, port: int, keepalive: int = 0) -> None:
        self.calls.append(f"connect:{host}:{port}")

    def loop_start(self) -> None:
        self.calls.append("loop_start")
        assert self.on_connect is not None
        self.on_connect(self, None, {}, self._connack, None)

    def subscribe(self, topic: str, qos: int) -> tuple[int, int]:
        self._mid += 1
        mid = self._mid
        self.calls.append(f"subscribe:{topic}")
        assert self.on_subscribe is not None
        code = (
            FakeReasonCode(0x80, True)
            if self._granted is None
            else FakeReasonCode(self._granted)
        )
        self.on_subscribe(self, None, mid, [code], None)
        return 0, mid

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")


def test_a_healthy_broker_reports_both_filters_granted() -> None:
    client = FakeBrokerClient()

    result = check_broker.check("127.0.0.1", 1883, "u", SECRET, 1.0, lambda: client)

    assert result.ok is True
    assert set(result.granted) == set(check_broker.SUBSCRIPTIONS)
    assert all(qos == 1 for qos in result.granted.values())


def test_a_refused_connection_is_not_ok() -> None:
    client = FakeBrokerClient(connack=FakeReasonCode(5, True))

    result = check_broker.check("127.0.0.1", 1883, "u", SECRET, 1.0, lambda: client)

    assert result.ok is False
    assert result.connected is False
    assert result.granted == {}


def test_a_refused_subscription_is_not_ok() -> None:
    client = FakeBrokerClient(granted=None)

    result = check_broker.check("127.0.0.1", 1883, "u", SECRET, 1.0, lambda: client)

    assert result.ok is False
    assert result.connected is True
    assert all(qos is None for qos in result.granted.values())


def test_a_qos_0_grant_is_not_good_enough() -> None:
    """The backend's acknowledgement design needs QoS 1."""
    client = FakeBrokerClient(granted=0)

    result = check_broker.check("127.0.0.1", 1883, "u", SECRET, 1.0, lambda: client)

    assert result.ok is False


def test_the_client_is_always_released() -> None:
    client = FakeBrokerClient(connack=FakeReasonCode(5, True))

    check_broker.check("127.0.0.1", 1883, "u", SECRET, 1.0, lambda: client)

    assert client.calls[-2:] == ["disconnect", "loop_stop"]


# -- the secret ------------------------------------------------------------


def test_the_password_goes_to_the_client_and_nowhere_else() -> None:
    client = FakeBrokerClient()

    result = check_broker.check("127.0.0.1", 1883, "backend", SECRET, 1.0, lambda: client)

    assert client.credentials == ("backend", SECRET)
    rendered = "\n".join(result.lines())
    assert SECRET not in rendered
    assert SECRET not in repr(result)
    assert not any(SECRET in call for call in client.calls)


def test_the_output_never_quotes_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERGUARD_BROKER_PASSWORD", SECRET)
    client = FakeBrokerClient()

    code = check_broker.main(["--username", "backend"], client_factory=lambda: client)

    captured = capsys.readouterr()
    assert code == 0
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "username       = backend" in captured.out


def test_a_connection_error_names_the_type_not_the_message() -> None:
    """An exception message could quote what was sent."""

    class Exploding(FakeBrokerClient):
        def connect(self, host: str, port: int, keepalive: int = 0) -> None:
            raise ConnectionRefusedError(f"refused for {SECRET}")

    client = Exploding()

    result = check_broker.check("127.0.0.1", 1883, "u", SECRET, 0.1, lambda: client)

    assert result.ok is False
    assert SECRET not in result.reason
    assert result.reason == "ConnectionRefusedError while connecting"


def test_it_takes_no_password_argument() -> None:
    args = check_broker.parse_args([])

    assert not hasattr(args, "password")
    assert args.password_env == check_broker.DEFAULT_PASSWORD_ENV
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--password"' not in source


def test_a_missing_password_is_refused_by_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("POWERGUARD_BROKER_PASSWORD", raising=False)

    assert check_broker.main([]) == 2

    error = capsys.readouterr().err
    assert "POWERGUARD_BROKER_PASSWORD" in error


def test_a_failed_check_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERGUARD_BROKER_PASSWORD", SECRET)
    client = FakeBrokerClient(connack=FakeReasonCode(5, True))

    assert check_broker.main([], client_factory=lambda: client) == 1
    assert SECRET not in capsys.readouterr().out


def test_the_real_client_disables_automatic_reconnect() -> None:
    client = check_broker._paho_client()

    assert client._reconnect_on_failure is False


# -- every required subscription must be confirmed, one for one ------------
#
# The regression: one SUBACK used to be enough. A broker that acknowledges
# telemetry and silently drops status would have looked healthy.


TELEMETRY, STATUS = check_broker.SUBSCRIPTIONS


class ScriptedClient:
    """A broker whose SUBACK behaviour each test writes itself.

    `answers` maps the order of the subscribe call (0, 1, ...) to what the
    broker does: a granted QoS, None for a refusal, or "silent" for no answer
    at all. `early` makes the answer arrive before `subscribe()` returns.
    """

    def __init__(
        self,
        answers: dict[int, Any],
        *,
        early: bool = False,
        connack: Any = 0,
        repeat_mid: int | None = None,
        extra_mid: int | None = None,
    ) -> None:
        self.on_connect: Any = None
        self.on_subscribe: Any = None
        self.credentials: tuple[str, str] | None = None
        self.calls: list[str] = []
        self._answers = answers
        self._early = early
        self._connack = connack
        self._repeat_mid = repeat_mid
        self._extra_mid = extra_mid
        self._index = 0
        self._mid = 100

    def username_pw_set(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def connect(self, host: str, port: int, keepalive: int = 0) -> None:
        self.calls.append("connect")

    def loop_start(self) -> int:
        self.calls.append("loop_start")
        assert self.on_connect is not None
        self.on_connect(self, None, {}, self._connack, None)
        if self._repeat_mid is not None:
            self._answer(self._repeat_mid, 1)
        if self._extra_mid is not None:
            self._answer(self._extra_mid, 1)
        return 0

    def subscribe(self, topic: str, qos: int) -> tuple[int, int]:
        index = self._index
        self._index += 1
        self._mid += 1
        mid = self._mid
        self.calls.append(f"subscribe:{topic}")
        answer = self._answers.get(index, 1)
        if answer != "silent" and self._early:
            # The broker answers before this call has returned its mid.
            self._answer(mid, answer)
            return 0, mid
        if answer != "silent":
            self._pending = (mid, answer)
        else:
            self._pending = None
        result = (0, mid)
        if self._pending is not None:
            self._answer(*self._pending)
        return result

    def _answer(self, mid: int, granted: Any) -> None:
        assert self.on_subscribe is not None
        code = (
            FakeReasonCode(0x80, True)
            if granted is None
            else FakeReasonCode(int(granted))
        )
        self.on_subscribe(self, None, mid, [code], None)

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def loop_stop(self) -> int:
        self.calls.append("loop_stop")
        return 0


def run_check(client: Any, timeout: float = 0.2) -> Any:
    return check_broker.check("127.0.0.1", 1883, "u", SECRET, timeout, lambda: client)


def test_both_required_subacks_pass() -> None:
    result = run_check(ScriptedClient({0: 1, 1: 1}))

    assert result.ok is True
    assert result.granted == {TELEMETRY: 1, STATUS: 1}
    assert result.missing == ()


def test_only_the_first_suback_times_out() -> None:
    result = run_check(ScriptedClient({0: 1, 1: "silent"}))

    assert result.ok is False
    assert result.granted == {TELEMETRY: 1}
    assert result.missing == (STATUS,)
    assert "unconfirmed" in result.reason


def test_only_the_second_suback_times_out() -> None:
    result = run_check(ScriptedClient({0: "silent", 1: 1}))

    assert result.ok is False
    assert result.granted == {STATUS: 1}
    assert result.missing == (TELEMETRY,)


def test_one_accepted_and_one_refused_fails() -> None:
    result = run_check(ScriptedClient({0: 1, 1: None}))

    assert result.ok is False
    assert result.granted == {TELEMETRY: 1, STATUS: None}
    assert result.missing == (), "both answered; one of them said no"


def test_a_qos_0_grant_alongside_a_qos_1_grant_fails() -> None:
    """QoS 0 removes the redelivery the acknowledgement design depends on."""
    result = run_check(ScriptedClient({0: 1, 1: 0}))

    assert result.ok is False
    assert result.granted == {TELEMETRY: 1, STATUS: 0}


def test_a_duplicate_suback_cannot_satisfy_the_other_subscription() -> None:
    """The exact defect: a repeat of one answer used to fill the other slot."""
    client = ScriptedClient({0: 1, 1: "silent"}, repeat_mid=101)

    result = run_check(client)

    assert result.ok is False
    assert result.granted == {TELEMETRY: 1}
    assert result.missing == (STATUS,)
    assert result.duplicates_ignored >= 1


def test_an_unrelated_mid_is_ignored() -> None:
    client = ScriptedClient({0: 1, 1: "silent"}, extra_mid=999)

    result = run_check(client)

    assert result.ok is False
    assert result.missing == (STATUS,)
    assert result.unrelated_ignored >= 1


def test_an_early_suback_is_still_matched_to_its_own_filter() -> None:
    """The broker may answer before subscribe() has returned the mid."""
    result = run_check(ScriptedClient({0: 1, 1: 1}, early=True))

    assert result.ok is True
    assert result.granted == {TELEMETRY: 1, STATUS: 1}


def test_an_early_partial_answer_still_fails() -> None:
    result = run_check(ScriptedClient({0: 1, 1: "silent"}, early=True))

    assert result.ok is False
    assert result.missing == (STATUS,)


def test_completion_is_reached_exactly_once() -> None:
    result = check_broker.Result(required=check_broker.SUBSCRIPTIONS)
    tracker = check_broker.SubscriptionTracker(check_broker.SUBSCRIPTIONS, result)

    for index, topic in enumerate(check_broker.SUBSCRIPTIONS):
        tracker.begin(topic)
        tracker.settle(10 + index)
    tracker.record(10, FakeReasonCode(1))
    assert tracker.complete is False
    assert tracker.completions == 0

    tracker.record(11, FakeReasonCode(1))
    assert tracker.complete is True
    assert tracker.completions == 1

    # More answers change nothing.
    tracker.record(10, FakeReasonCode(1))
    tracker.record(11, FakeReasonCode(1))
    assert tracker.completions == 1
    assert result.duplicates_ignored == 2


def test_a_partial_result_never_claims_success_in_its_output() -> None:
    result = run_check(ScriptedClient({0: 1, 1: "silent"}))
    rendered = "\\n".join(result.lines())

    assert "no SUBACK" in rendered
    assert SECRET not in rendered


def test_a_partial_check_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERGUARD_BROKER_PASSWORD", SECRET)
    client = ScriptedClient({0: 1, 1: "silent"})

    code = check_broker.main(["--timeout", "0.2"], client_factory=lambda: client)

    out = capsys.readouterr().out
    assert code == 1
    assert "no SUBACK" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, 0), (1, 1), (2, 2), (0x80, None), (None, None), ("nonsense", None)],
)
def test_granted_qos_reads_a_suback_entry(code: Any, expected: Any) -> None:
    assert check_broker.granted_qos(code) == expected
