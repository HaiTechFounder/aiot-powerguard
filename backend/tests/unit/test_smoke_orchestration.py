"""The broker smoke's own guard rails, checked without a broker.

The smoke test is skipped on every ordinary run, so the logic that decides
*whether* to skip — and the command it prints when it does — would otherwise
never be exercised. A wrong skip reason is how a NOT_RUN quietly turns into a
believed PASS.
"""

from __future__ import annotations

import socket

import pytest

from tests.integration import test_mosquitto_smoke as smoke


def test_the_module_is_marked_integration() -> None:
    assert smoke.pytestmark.name == "integration"


def test_it_skips_when_the_switch_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(smoke.ENABLE, raising=False)

    reason = smoke.skip_reason()

    assert reason is not None
    assert "NOT_RUN" in reason
    assert smoke.ENABLE_COMMAND in reason


def test_it_skips_when_the_password_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(smoke.ENABLE, "1")
    monkeypatch.delenv("POWERGUARD_SMOKE_PASSWORD", raising=False)

    reason = smoke.skip_reason()

    assert reason is not None
    assert "NOT_RUN" in reason
    assert "POWERGUARD_SMOKE_PASSWORD" in reason


def test_it_skips_when_nothing_is_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(smoke.ENABLE, "1")
    monkeypatch.setenv("POWERGUARD_SMOKE_PASSWORD", "placeholder")
    monkeypatch.setattr(smoke, "broker_reachable", lambda *_a, **_k: False)

    reason = smoke.skip_reason()

    assert reason is not None
    assert "NOT_RUN" in reason
    assert "deploy/mosquitto/README.md" in reason


def test_it_runs_only_when_everything_is_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(smoke.ENABLE, "1")
    monkeypatch.setenv("POWERGUARD_SMOKE_PASSWORD", "placeholder")
    monkeypatch.setattr(smoke, "broker_reachable", lambda *_a, **_k: True)

    assert smoke.skip_reason() is None


def test_the_enabling_command_names_no_real_credential() -> None:
    assert "<password>" in smoke.ENABLE_COMMAND
    assert "POWERGUARD_SMOKE_BROKER" in smoke.ENABLE_COMMAND
    assert "-m integration" in smoke.ENABLE_COMMAND


def test_reachability_is_a_bounded_probe() -> None:
    """A closed port must answer quickly, not hang the collection phase."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert smoke.broker_reachable("127.0.0.1", free_port, timeout=0.2) is False


def test_it_probes_the_configured_endpoint_not_a_hard_coded_one() -> None:
    assert smoke.HOST
    assert isinstance(smoke.PORT, int)
    # 1883 is what deploy/mosquitto publishes; see tests/unit/test_broker_config.py.
    assert smoke.PORT == 1883
