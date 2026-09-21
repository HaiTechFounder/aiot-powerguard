"""The synthetic publisher, checked against the backend's own contract.

Every valid payload a scenario produces is run through `validate_message` — the
same function ingestion uses — so "the publisher emits what the device emits"
is a fact rather than a claim, and the two cannot drift apart silently.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from powerguard.config import Settings
from powerguard.mqtt.validation import Invalid, RejectionReason, ValidStatus, ValidTelemetry
from powerguard.mqtt.validation import validate_message as validate
from tests.conftest import make_settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "publish_synthetic.py"


def load_publisher() -> ModuleType:
    """Import the script by path: it is a tool, not an installed module."""
    spec = importlib.util.spec_from_file_location("publish_synthetic", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `dataclass` resolves a class's module from
    # sys.modules while processing it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publisher = load_publisher()
ALL_SCENARIOS = list(publisher.SCENARIOS)


@pytest.fixture
def settings() -> Settings:
    return make_settings("sqlite:///./data/test.db")


def messages(scenario: str, **overrides: Any) -> list[Any]:
    options: dict[str, Any] = {
        "device_id": "powerguard-01",
        "firmware": "0.1.0",
        "boot": "7fa31c09",
        "count": 6,
        "start_seq": 1,
        "rng": random.Random(11),
    }
    options.update(overrides)
    return publisher.build_messages(scenario, **options)


def make_plan(*argv: str) -> Any:
    """A full plan, the way the CLI would build it."""
    return publisher.plan(publisher.parse_args(["--dry-run", *argv]))


def rendered(*argv: str) -> list[str]:
    return publisher.render(make_plan(*argv))


def payloads(items: list[Any]) -> list[dict[str, Any]]:
    """Every body that is a JSON object. Malformed bodies are skipped."""
    documents = []
    for item in items:
        try:
            parsed = json.loads(item.body)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            documents.append(parsed)
    return documents


def readings(items: list[Any]) -> list[dict[str, Any]]:
    """Telemetry documents only: status payloads carry no sequence."""
    return [document for document in payloads(items) if "seq" in document]


# -- CLI -------------------------------------------------------------------


def test_the_nine_specified_scenarios_are_offered() -> None:
    assert ALL_SCENARIOS == [
        "normal",
        "duplicate",
        "spike",
        "drift",
        "gap",
        "out-of-order",
        "malformed",
        "status",
        "reconnect",
    ]


def test_help_documents_every_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        publisher.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for name, description in publisher.SCENARIOS.items():
        assert name in out
        assert description in out


def test_list_scenarios_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert publisher.main(["--list-scenarios"]) == 0
    assert "out-of-order" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["all", "normal", "normal,gap", " spike , drift "])
def test_scenario_selection_accepts_a_name_a_list_or_all(value: str) -> None:
    assert publisher.resolve_scenarios(value)


def test_an_unknown_scenario_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        publisher.resolve_scenarios("chaos")


def test_a_bad_scenario_exits_two_without_publishing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert publisher.main(["--dry-run", "--scenario", "chaos"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


@pytest.mark.parametrize("device_id", ["Bad-Caps", "with space", "", "x" * 40, "-leading"])
def test_a_device_id_outside_the_grammar_is_refused(device_id: str) -> None:
    args = publisher.parse_args(["--dry-run", f"--device-id={device_id}"])
    with pytest.raises(ValueError, match="device id"):
        publisher.plan(args)


def test_count_must_be_positive() -> None:
    args = publisher.parse_args(["--dry-run", "--count", "0"])
    with pytest.raises(ValueError, match="count"):
        publisher.plan(args)


# -- dry run ---------------------------------------------------------------


def test_dry_run_prints_payloads_and_publishes_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("dry run must not touch the network")

    monkeypatch.setattr("socket.socket", explode)

    assert publisher.main(["--dry-run", "--scenario", "all", "--count", "3"]) == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines
    header = [line for line in lines if line.startswith("#")]
    # Two header lines per session, plus the closing graceful-offline line.
    assert len(header) >= 3
    assert header[0].startswith("# session 1 ")
    assert header[-1].startswith("# graceful offline ")
    for line in lines:
        if line.startswith("#"):
            continue
        body = line.removeprefix("DROP ")
        assert body.startswith("powerguard/v1/devices/powerguard-01/")


def test_a_seed_makes_a_run_reproducible() -> None:
    first = messages("normal", rng=random.Random(5))
    second = messages("normal", rng=random.Random(5))
    assert [m.body for m in first] == [m.body for m in second]


# -- topic and credential safety -------------------------------------------


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_a_scenario_only_addresses_its_own_device_topics(scenario: str) -> None:
    allowed = {
        "powerguard/v1/devices/dev-7/telemetry",
        "powerguard/v1/devices/dev-7/status",
    }
    for message in messages(scenario, device_id="dev-7"):
        assert message.topic in allowed


def test_the_password_is_only_ever_read_from_the_environment() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--password-env" in source
    assert '"--password"' not in source, "a password flag would reach shell history"

    out = publisher.parse_args([])
    assert not hasattr(out, "password")


def test_a_missing_password_refuses_to_publish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("POWERGUARD_DEVICE_PASSWORD", raising=False)

    assert publisher.main(["--username", "device", "--host", "127.0.0.1"]) == 2

    error = capsys.readouterr().err
    assert "POWERGUARD_DEVICE_PASSWORD" in error, "names the variable, not a value"


def test_no_payload_carries_a_credential_field() -> None:
    for scenario in ALL_SCENARIOS:
        for document in payloads(messages(scenario)):
            for key in document:
                assert "password" not in key.lower()
                assert "secret" not in key.lower()
                assert "token" not in key.lower()


# -- generated payloads satisfy the backend contract -----------------------


@pytest.mark.parametrize(
    "scenario", ["normal", "duplicate", "spike", "drift", "gap", "out-of-order"]
)
def test_valid_scenarios_are_accepted_by_the_backend_validator(
    scenario: str, settings: Settings
) -> None:
    items = messages(scenario, count=8)
    assert items
    for message in items:
        result = validate(message.topic, message.body.encode(), settings)
        assert isinstance(result, ValidTelemetry), f"{message.label}: {result}"


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_no_scenario_breaches_the_hardware_contract(scenario: str) -> None:
    for document in readings(messages(scenario, count=12)):
        if "extra_field" in document or document["schema_version"] != 1:
            continue  # a deliberately malformed sample
        voltage = document["voltage_v"]
        if voltage > publisher.PACK_CEILING_V:
            assert scenario == "malformed", "only the malformed scenario may exceed 8.4 V"
        else:
            assert 0.0 <= voltage <= publisher.PACK_CEILING_V
            assert document["energy_wh"] >= 0.0


def test_status_scenario_brackets_telemetry_with_online_and_offline(
    settings: Settings,
) -> None:
    items = messages("status", count=3)

    assert items[0].label == "status online"
    assert items[0].retain is True
    assert items[-1].label == "graceful offline"
    for message in (items[0], items[-1]):
        result = validate(message.topic, message.body.encode(), settings, retained=True)
        assert isinstance(result, ValidStatus)
    assert json.loads(items[0].body)["status"] == "online"
    assert json.loads(items[-1].body)["status"] == "offline"


def test_duplicate_scenario_repeats_an_identical_reading() -> None:
    items = messages("duplicate", count=4)
    duplicates = [m for m in items if m.label == "duplicate"]
    assert duplicates

    for duplicate in duplicates:
        twin = [m for m in items if m.body == duplicate.body]
        assert len(twin) == 2, "the repeat must be byte-identical"


def test_gap_scenario_skips_sequence_numbers() -> None:
    sequence = [document["seq"] for document in readings(messages("gap", count=8))]
    steps = {b - a for a, b in pairwise(sequence)}
    assert steps != {1}, "a gap scenario that never skips proves nothing"
    assert max(steps) > 1


def test_out_of_order_scenario_delivers_a_later_sequence_first() -> None:
    sequence = [document["seq"] for document in readings(messages("out-of-order", count=9))]
    assert any(b < a for a, b in pairwise(sequence))
    # Nothing is invented: every sequence number still appears exactly once.
    assert len(sequence) == len(set(sequence))


def test_spike_scenario_produces_one_surge_within_the_contract() -> None:
    documents = readings(messages("spike", count=9))
    currents = [document["current_a"] for document in documents]
    assert max(currents) > 3.0
    assert max(document["voltage_v"] for document in documents) <= publisher.PACK_CEILING_V


def test_drift_scenario_sags_monotonically() -> None:
    voltages = [document["voltage_v"] for document in readings(messages("drift", count=8))]
    assert voltages[0] > voltages[-1]
    assert voltages[-1] < 7.0


def test_reconnect_scenario_restarts_the_boot_and_the_sequence() -> None:
    items = messages("reconnect", count=6)
    labels = [m.label for m in items]
    assert "reboot online" in labels

    documents = readings(items)
    boots = {document["boot_id"] for document in documents}
    assert len(boots) == 2, "a reboot means a new boot id"
    after = [d for d in documents if d["boot_id"] != documents[0]["boot_id"]]
    assert after[0]["seq"] == 1
    assert after[0]["energy_wh"] == 0.0, "energy never carries across a boot"


def test_malformed_scenario_is_rejected_for_the_stated_reasons(
    settings: Settings,
) -> None:
    items = messages("malformed", count=9)
    assert items

    reasons = set()
    for message in items:
        result = validate(message.topic, message.body.encode(), settings)
        assert isinstance(result, Invalid), message.label
        reasons.add(result.reason)

    assert RejectionReason.JSON_INVALID in reasons
    assert RejectionReason.SCHEMA_INVALID in reasons
    assert RejectionReason.VERSION_UNSUPPORTED in reasons
    assert RejectionReason.MEASUREMENT_OUT_OF_RANGE in reasons


def test_a_run_is_bounded_however_large_the_count_is() -> None:
    args = publisher.parse_args(["--dry-run", "--scenario", "all", "--count", "100000"])
    assert len(publisher.plan(args).messages) <= publisher.MAX_MESSAGES


# -- a seeded run reproduces completely ------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--scenario", "all", "--count", "4"],
        ["--scenario", "reconnect", "--count", "6"],
        ["--scenario", "normal", "--count", "5", "--with-timestamp"],
        ["--scenario", "gap,drift", "--count", "3", "--device-id", "dev-9"],
    ],
)
def test_the_same_seed_reproduces_the_whole_run(argv: list[str]) -> None:
    """Not just the measurements: boot ids, topics, order and bytes."""
    first = make_plan(*argv, "--seed", "42")
    second = make_plan(*argv, "--seed", "42")

    assert first == second
    assert first.boot_id == second.boot_id
    assert first.will_body == second.will_body
    assert [m.body for m in first.messages] == [m.body for m in second.messages]
    assert [m.topic for m in first.messages] == [m.topic for m in second.messages]
    assert [m.action for m in first.messages] == [m.action for m in second.messages]


def test_a_seeded_run_is_reproducible_through_the_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    publisher.main(["--dry-run", "--scenario", "all", "--count", "3", "--seed", "5"])
    first = capsys.readouterr().out
    publisher.main(["--dry-run", "--scenario", "all", "--count", "3", "--seed", "5"])
    second = capsys.readouterr().out

    assert first == second
    assert first.strip(), "a plan that prints nothing proves nothing"


def test_a_different_seed_changes_the_randomised_fields() -> None:
    first = make_plan("--scenario", "all", "--count", "4", "--seed", "1")
    second = make_plan("--scenario", "all", "--count", "4", "--seed", "2")

    assert first.boot_id != second.boot_id
    assert [m.body for m in first.messages] != [m.body for m in second.messages]
    # The shape is the seed's business; the contract is not.
    assert [m.topic for m in first.messages] == [m.topic for m in second.messages]
    assert [m.action for m in first.messages] == [m.action for m in second.messages]
    assert [m.label for m in first.messages] == [m.label for m in second.messages]


def test_planning_reads_no_clock_and_no_global_random_state() -> None:
    """Two plans built either side of a reseeded global RNG must match."""
    random.seed(1234)
    first = make_plan("--scenario", "all", "--count", "3", "--seed", "8")
    random.seed(999)
    second = make_plan("--scenario", "all", "--count", "3", "--seed", "8")

    assert first == second


def test_logical_timestamps_are_derived_not_read_from_a_clock() -> None:
    documents = readings(
        messages("normal", count=4, with_timestamp=True, interval=2.0)
    )
    stamps = [document["sampled_at"] for document in documents]

    assert stamps == [
        "2026-01-01T00:00:00.000Z",
        "2026-01-01T00:00:02.000Z",
        "2026-01-01T00:00:04.000Z",
        "2026-01-01T00:00:06.000Z",
    ]


def test_a_logical_timestamp_is_accepted_by_the_backend(settings: Settings) -> None:
    for message in messages("normal", count=3, with_timestamp=True):
        result = validate(message.topic, message.body.encode(), settings)
        assert isinstance(result, ValidTelemetry)


# -- one session, one boot id ----------------------------------------------


def test_one_session_uses_one_boot_id_everywhere() -> None:
    """Telemetry, status and the will all belong to the same boot."""
    run = make_plan("--scenario", "status", "--count", "4", "--seed", "3")

    assert len(run.sessions) == 1
    assert json.loads(run.will_body)["boot_id"] == run.boot_id
    assert json.loads(run.will_body)["status"] == "offline"
    for message in run.messages:
        document = json.loads(message.body)
        assert document["boot_id"] == run.boot_id


def test_the_will_is_never_a_fresh_boot_id() -> None:
    """The regression: the will used to be generated separately at connect."""
    for seed in ("1", "2", "3"):
        run = make_plan("--scenario", "normal", "--count", "2", "--seed", seed)
        assert json.loads(run.will_body)["boot_id"] == run.boot_id


def test_only_a_deliberate_reboot_introduces_a_second_boot_id() -> None:
    run = make_plan("--scenario", "reconnect", "--count", "6", "--seed", "3")
    boots = [json.loads(m.body)["boot_id"] for m in run.messages]

    assert boots[0] == run.boot_id
    # The drop belongs to the session being killed, not to the new one.
    drop = next(m for m in run.messages if m.action == publisher.DROP)
    assert json.loads(drop.body)["boot_id"] == run.boot_id
    assert len(set(boots)) == 2


def test_the_drop_step_carries_the_will_of_the_session_it_kills() -> None:
    run = make_plan("--scenario", "reconnect", "--count", "4", "--seed", "11")
    drop = next(m for m in run.messages if m.action == publisher.DROP)

    assert drop.body == run.sessions[0].will_body
    assert drop.retain is True
    assert "broker publishes the will" in drop.label


def test_no_scenario_publishes_a_will_payload_by_hand() -> None:
    """An offline payload the device sends is graceful, and says so.

    Manually publishing what the broker is supposed to publish would prove
    nothing about the will, so the unexpected-drop step is an action, not a
    publication.
    """
    for scenario in ALL_SCENARIOS:
        for message in messages(scenario):
            try:
                document = json.loads(message.body)
            except json.JSONDecodeError:
                continue  # a deliberately malformed sample
            if document.get("status") == "offline" and message.action == publisher.PUBLISH:
                assert message.label == "graceful offline"


def test_the_status_scenario_ends_with_a_graceful_offline() -> None:
    run = make_plan("--scenario", "status", "--count", "3", "--seed", "4")

    assert run.messages[-1].label == "graceful offline"
    assert run.messages[-1].action == publisher.PUBLISH
    assert all(m.action == publisher.PUBLISH for m in run.messages)


# -- QoS 1 delivery is confirmed before a graceful disconnect ---------------


class FakePublishInfo:
    def __init__(self, published: bool = True, waited: bool = True) -> None:
        self._published = published
        self._waited = waited
        self.wait_calls: list[float | None] = []

    def wait_for_publish(self, timeout: float | None = None) -> None:
        self.wait_calls.append(timeout)

    def is_published(self) -> bool:
        return self._published


class FakeDeviceClient:
    """Only the calls the publisher makes on a Paho client."""

    def __init__(self, published: bool = True) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.infos: list[FakePublishInfo] = []
        self._published_result = published

    def publish(self, topic: str, body: str, qos: int = 0, retain: bool = False) -> Any:
        self.published.append((topic, body, qos, retain))
        info = FakePublishInfo(self._published_result)
        self.infos.append(info)
        return info


def test_a_graceful_publication_waits_for_the_broker_to_confirm() -> None:
    client = FakeDeviceClient()
    message = publisher.Message("powerguard/v1/devices/x/status", "{}", retain=True)

    publisher.publish_confirmed(client, message, timeout=2.5)

    assert client.published == [("powerguard/v1/devices/x/status", "{}", 1, True)]
    assert client.infos[0].wait_calls == [2.5], "bounded, never an unbounded wait"


def test_an_unconfirmed_publication_is_reported_not_assumed() -> None:
    """A timeout must never be reported as a successful goodbye."""
    client = FakeDeviceClient(published=False)
    message = publisher.Message("powerguard/v1/devices/x/status", "{}", retain=True)

    with pytest.raises(publisher.PublishNotConfirmedError, match="not confirmed"):
        publisher.publish_confirmed(client, message, timeout=0.5)


def test_the_publish_timeout_is_bounded_and_small() -> None:
    assert 0 < publisher.PUBLISH_TIMEOUT_S <= 30


# -- the plan renders every step -------------------------------------------


def test_the_rendering_names_the_session_and_its_will() -> None:
    run = make_plan("--scenario", "reconnect", "--count", "4", "--seed", "6")
    lines = publisher.render(run)

    assert lines[0].startswith(f"# session 1 boot_id={run.boot_id}")
    assert run.boot_id in lines[1]
    assert any(line.startswith("DROP ") for line in lines), "the drop is visible"
    assert any(line.startswith("# session 2 ") for line in lines)
    # Two header lines per session, plus the closing graceful-offline line.
    assert len(lines) == len(run.messages) + 2 * len(run.sessions) + 1


# -- a reconnect establishes a new session identity ------------------------
#
# The regression: after a reconnect the will and the final offline still
# described the session that had already died.


def reconnect_plan(seed: str = "7") -> Any:
    return make_plan("--scenario", "reconnect", "--count", "6", "--seed", seed)


def test_a_reconnect_splits_the_run_into_two_sessions() -> None:
    run = reconnect_plan()

    assert len(run.sessions) == 2
    assert run.sessions[0].boot_id != run.sessions[1].boot_id
    assert run.sessions[0].ends_with_drop is True
    assert run.sessions[1].ends_with_drop is False


def test_session_a_owns_everything_up_to_its_drop() -> None:
    first = reconnect_plan().sessions[0]

    assert json.loads(first.will_body)["boot_id"] == first.boot_id
    for message in first.messages:
        assert json.loads(message.body)["boot_id"] == first.boot_id
    assert first.messages[-1].action == publisher.DROP


def test_session_b_has_its_own_will_and_telemetry() -> None:
    run = reconnect_plan()
    second = run.sessions[1]

    assert json.loads(second.will_body)["boot_id"] == second.boot_id
    assert json.loads(second.will_body)["status"] == "offline"
    for message in second.messages:
        assert json.loads(message.body)["boot_id"] == second.boot_id


def test_the_final_offline_belongs_to_the_session_that_is_connected() -> None:
    run = reconnect_plan()

    assert run.final_session is run.sessions[1]
    assert json.loads(run.final_session.will_body)["boot_id"] == run.sessions[1].boot_id
    assert run.final_session.will_body != run.sessions[0].will_body


def test_the_old_boot_id_is_never_used_after_the_reconnect() -> None:
    run = reconnect_plan()
    dead = run.sessions[0].boot_id
    after_drop = run.sessions[1].messages

    assert after_drop, "the reboot must publish something"
    assert all(json.loads(m.body)["boot_id"] != dead for m in after_drop)
    assert dead not in run.final_session.will_body


def test_a_later_scenario_continues_in_the_replacement_session() -> None:
    """`--scenario reconnect,normal` must not fall back to the dead identity."""
    run = make_plan("--scenario", "reconnect,normal", "--count", "4", "--seed", "7")
    dead = run.sessions[0].boot_id

    assert len(run.sessions) == 2
    assert all(json.loads(m.body)["boot_id"] != dead for m in run.sessions[1].messages)
    assert run.sessions[1].messages[-1].label == "normal"


def test_the_sequence_of_session_ids_is_deterministic() -> None:
    first = reconnect_plan("13")
    second = reconnect_plan("13")

    assert first.boot_ids == second.boot_ids
    assert len(set(first.boot_ids)) == len(first.boot_ids), "each session is distinct"
    assert first == second


def test_a_different_seed_changes_both_session_ids() -> None:
    first = reconnect_plan("1")
    second = reconnect_plan("2")

    assert first.boot_ids != second.boot_ids
    assert first.boot_ids[0] != second.boot_ids[0]
    assert first.boot_ids[1] != second.boot_ids[1]


# -- the publisher owns reconnection ---------------------------------------


def test_paho_automatic_reconnect_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise Paho's retry would race the deliberate reconnect in execute()."""
    monkeypatch.setenv("POWERGUARD_DEVICE_PASSWORD", "placeholder")
    args = publisher.parse_args(["--username", "device"])

    client = publisher.build_client(args, "powerguard/v1/devices/powerguard-01/status", "{}")

    assert client._reconnect_on_failure is False
    assert client._client_id == b"powerguard-device-powerguard-01"


def test_the_client_registers_the_first_session_will(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERGUARD_DEVICE_PASSWORD", "placeholder")
    args = publisher.parse_args(["--username", "device"])
    will = '{"schema_version": 1, "status": "offline"}'

    client = publisher.build_client(args, "powerguard/v1/devices/powerguard-01/status", will)

    assert client._will is True
    assert client._will_payload == will.encode()
    assert client._will_qos == 1
    assert client._will_retain is True


# -- execution: sessions, exit codes, no overlapping reconnects ------------


class RecordingClient:
    """The calls `execute` makes, in order, with nothing else attached.

    It also models the one Paho rule that matters at a session handoff: a
    second `loop_start()` is refused while a loop is already running.
    """

    def __init__(
        self,
        *,
        confirm: bool = True,
        confirm_offline: bool = True,
        loop_start_result: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.wills: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.loops_running = 0
        self.max_loops_running = 0
        self._confirm = confirm
        self._confirm_offline = confirm_offline
        self._forced_loop_result = loop_start_result
        self._seen_drop = False

    def will_set(self, topic: str, body: str, qos: int = 0, retain: bool = False) -> None:
        self.calls.append("will_set")
        self.wills.append(body)

    def publish(self, topic: str, body: str, qos: int = 0, retain: bool = False) -> Any:
        self.calls.append("publish")
        self.published.append((topic, body))
        graceful = json.loads(body).get("status") == "offline" if body.startswith("{") else False
        confirmed = self._confirm
        if graceful and self._seen_drop is False and self._confirm_offline is False:
            confirmed = False
        return FakePublishInfo(confirmed)

    def reconnect(self) -> None:
        self.calls.append("reconnect")

    def loop_start(self) -> int:
        self.calls.append("loop_start")
        if self._forced_loop_result is not None:
            return self._forced_loop_result
        if self.loops_running:
            # What Paho does: MQTT_ERR_INVAL while a thread already exists.
            return 4
        self.loops_running += 1
        self.max_loops_running = max(self.max_loops_running, self.loops_running)
        return 0

    def loop_stop(self) -> int:
        self.calls.append("loop_stop")
        if not self.loops_running:
            return 4
        self.loops_running -= 1
        return 0

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def _sock_close(self) -> None:
        self.calls.append("drop")
        self._seen_drop = True


def run_plan(run: Any, client: RecordingClient) -> int:
    """`execute` is always entered with a loop already running."""
    client.loop_start()
    client.calls.clear()
    return publisher.execute(run, client, interval=0.0, sleep=lambda _s: None)


def test_execution_registers_the_next_session_will_before_reconnecting() -> None:
    run = reconnect_plan()
    client = RecordingClient()

    assert run_plan(run, client) == 0

    drop = client.calls.index("drop")
    will = client.calls.index("will_set", drop)
    reconnect = client.calls.index("reconnect", drop)
    assert drop < will < reconnect, "the broker must never hold a dead session's will"
    assert client.wills == [run.sessions[1].will_body]


def test_execution_restarts_the_network_loop_after_a_reconnect() -> None:
    """With Paho's own reconnect off, its loop ends with the connection."""
    client = RecordingClient()

    run_plan(reconnect_plan(), client)

    reconnect = client.calls.index("reconnect")
    assert client.calls[reconnect + 1] == "loop_start"
    assert client.calls.count("reconnect") == 1, "one reconnect, not a race"


def test_the_graceful_offline_uses_the_final_session() -> None:
    run = reconnect_plan()
    client = RecordingClient()

    run_plan(run, client)

    last_topic, last_body = client.published[-1]
    assert last_topic.endswith("/status")
    assert last_body == run.final_session.will_body
    assert json.loads(last_body)["boot_id"] == run.sessions[1].boot_id
    assert client.calls[-2:] == ["disconnect", "loop_stop"]


def test_a_confirmed_goodbye_exits_zero() -> None:
    client = RecordingClient(confirm=True)

    assert run_plan(make_plan("--scenario", "normal", "--count", "3"), client) == 0


def test_an_unconfirmed_goodbye_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The regression: a failed final offline used to exit 0."""
    run = make_plan("--scenario", "normal", "--count", "2", "--seed", "4")
    client = RecordingClient(confirm_offline=False)

    code = run_plan(run, client)

    assert code != 0
    error = capsys.readouterr().err
    assert "graceful offline not confirmed" in error
    assert "loop_stop" in client.calls, "the client is still released"


def test_an_unconfirmed_telemetry_publication_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = make_plan("--scenario", "normal", "--count", "2", "--seed", "4")
    client = RecordingClient(confirm=False)

    assert run_plan(run, client) != 0
    assert "publish not confirmed" in capsys.readouterr().err


def test_the_cli_returns_the_execution_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through main(), with only the client replaced."""
    monkeypatch.setenv("POWERGUARD_DEVICE_PASSWORD", "placeholder")
    failing = RecordingClient(confirm_offline=False)

    class Connectable(RecordingClient):
        def connect(self, host: str, port: int, keepalive: int = 0) -> None:
            self.calls.append("connect")

    good = Connectable()
    bad = Connectable(confirm_offline=False)

    ok = publisher.main(
        ["--username", "device", "--count", "2", "--interval", "0"],
        client_factory=lambda *_a: good,
    )
    failed = publisher.main(
        ["--username", "device", "--count", "2", "--interval", "0"],
        client_factory=lambda *_a: bad,
    )

    assert ok == 0
    assert failed != 0
    assert "graceful offline not confirmed" in capsys.readouterr().err
    del failing


# -- no credential ever reaches an argument list ---------------------------


def test_the_publisher_takes_no_password_argument() -> None:
    args = publisher.parse_args([])

    assert not hasattr(args, "password")
    assert "--password-env" in SCRIPT.read_text(encoding="utf-8")


def test_no_document_passes_a_password_on_a_command_line() -> None:
    """`mosquitto_sub -P <password>` would expose it in the process listing."""
    backend = SCRIPT.resolve().parents[1]
    documents = [
        backend / "README.md",
        backend / "docs" / "OPERATIONS.md",
        backend / "docs" / "TESTING.md",
        backend / "deploy" / "mosquitto" / "README.md",
        backend / "tests" / "integration" / "README.md",
    ]

    for document in documents:
        text = document.read_text(encoding="utf-8")
        for flag in (" -P ", " --pw ", " -pw "):
            assert flag not in text, f"{document.name} passes a password in argv"
        assert "mosquitto_sub -P" not in text
        assert "mosquitto_pub -P" not in text


# -- the network loop is handed over, not doubled up -----------------------
#
# Paho refuses a second `loop_start()` while a thread exists, and `loop_stop()`
# joins the old one. Getting the order wrong leaves the replacement session
# with no loop at all, publishing into a buffer nothing drains.


def test_a_drop_retires_the_old_loop_before_opening_a_new_one() -> None:
    """Case A and B: stop, reconnect, then exactly one start."""
    client = RecordingClient()

    assert run_plan(reconnect_plan(), client) == 0

    drop = client.calls.index("drop")
    stop = client.calls.index("loop_stop", drop)
    reconnect = client.calls.index("reconnect", drop)
    start = client.calls.index("loop_start", drop)
    assert drop < stop < reconnect < start


def test_only_one_network_loop_runs_at_a_time() -> None:
    """Case C: two loops on one client would interleave socket reads."""
    client = RecordingClient()

    assert run_plan(reconnect_plan(), client) == 0

    assert client.max_loops_running == 1
    assert client.loops_running == 0, "and none is left running at the end"


def test_a_refused_loop_start_fails_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Case D: a reconnect whose loop never started did not succeed."""
    client = RecordingClient(loop_start_result=4)

    code = run_plan(reconnect_plan(), client)

    assert code != 0
    error = capsys.readouterr().err
    assert "reconnect failed" in error
    assert "loop_start refused with 4" in error


def test_a_successful_handoff_continues_the_run() -> None:
    """Case E: the replacement session keeps publishing."""
    run = reconnect_plan()
    client = RecordingClient()

    assert run_plan(run, client) == 0

    after = [body for _topic, body in client.published]
    assert any(
        json.loads(body).get("boot_id") == run.sessions[1].boot_id for body in after
    )
    assert client.published[-1][1] == run.final_session.will_body


def test_start_loop_accepts_only_success() -> None:
    class Refusing:
        def loop_start(self) -> int:
            return 4

    class Silent:
        def loop_start(self) -> None:
            return None

    with pytest.raises(publisher.LoopHandoffError):
        publisher.start_loop(Refusing())
    with pytest.raises(publisher.LoopHandoffError):
        # A client that reports nothing has not reported success.
        publisher.start_loop(Silent())


def test_the_handoff_registers_the_will_between_stop_and_reconnect() -> None:
    client = RecordingClient()
    client.loop_start()
    client.calls.clear()

    publisher.handoff(client, "powerguard/v1/devices/x/status", "{}")

    assert client.calls == ["loop_stop", "will_set", "reconnect", "loop_start"]
    assert client.wills == ["{}"]


def test_shutdown_leaves_no_network_thread_running() -> None:
    """Case G, on both the clean and the failing path."""
    for client in (RecordingClient(), RecordingClient(confirm_offline=False)):
        run_plan(make_plan("--scenario", "normal", "--count", "2"), client)

        assert client.loops_running == 0
        assert client.calls[-1] == "loop_stop"


def test_a_failed_initial_loop_start_fails_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POWERGUARD_DEVICE_PASSWORD", "placeholder")

    class Connectable(RecordingClient):
        def connect(self, host: str, port: int, keepalive: int = 0) -> None:
            self.calls.append("connect")

    client = Connectable(loop_start_result=4)

    code = publisher.main(
        ["--username", "device", "--count", "2", "--interval", "0"],
        client_factory=lambda *_a: client,
    )

    assert code != 0
    assert "could not start the network loop" in capsys.readouterr().err


def test_no_hidden_paho_automatic_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case F, restated where the loop handling lives."""
    monkeypatch.setenv("POWERGUARD_DEVICE_PASSWORD", "placeholder")
    args = publisher.parse_args(["--username", "device"])

    client = publisher.build_client(args, "powerguard/v1/devices/powerguard-01/status", "{}")

    assert client._reconnect_on_failure is False
    source = SCRIPT.read_text(encoding="utf-8")
    assert "reconnect_on_failure=False" in source
