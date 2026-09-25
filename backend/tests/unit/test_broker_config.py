"""The broker configuration and the instructions that describe it.

Documentation drift is what this guards. The previous configuration bound the
container's listener to 127.0.0.1, which is its own loopback, so the published
port could never reach it — and the README happily told people to run it. Every
path, port and file name a document mentions is checked against the file that
actually defines it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
DEPLOY = BACKEND / "deploy" / "mosquitto"
COMPOSE = BACKEND / "compose.yaml"
NATIVE_CONF = DEPLOY / "mosquitto.native.conf"
DOCKER_CONF = DEPLOY / "mosquitto.docker.conf"
ACL = DEPLOY / "acl"
BROKER_README = DEPLOY / "README.md"
BACKEND_README = BACKEND / "README.md"
OPERATIONS = BACKEND / "docs" / "OPERATIONS.md"

MQTT_PORT = 1883


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def directives(conf: Path) -> dict[str, str]:
    """The `key value` lines of a mosquitto configuration, comments dropped."""
    found: dict[str, str] = {}
    for line in read(conf).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(" ")
        found[key] = value.strip()
    return found


# -- the files exist at the names everything references --------------------


@pytest.mark.parametrize(
    "path", [COMPOSE, NATIVE_CONF, DOCKER_CONF, ACL, BROKER_README]
)
def test_every_referenced_file_exists(path: Path) -> None:
    assert path.is_file(), f"{path.name} is referenced but missing"


def test_there_is_no_ambiguous_single_config_left() -> None:
    """One file for two bind addresses is what caused the original defect."""
    assert not (DEPLOY / "mosquitto.conf").exists()


# -- the listeners bind where each path needs them to ----------------------


def test_the_native_listener_binds_loopback() -> None:
    assert directives(NATIVE_CONF)["listener"] == f"{MQTT_PORT} 127.0.0.1"


def test_the_container_listener_binds_all_interfaces() -> None:
    """Inside a container, a loopback bind is unreachable from a published port."""
    assert directives(DOCKER_CONF)["listener"] == f"{MQTT_PORT} 0.0.0.0"


def test_only_the_mqtt_port_is_published() -> None:
    compose = read(COMPOSE)
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"', compose, re.MULTILINE)

    assert len(published) == 1
    assert published[0].endswith(f":{MQTT_PORT}:{MQTT_PORT}")


def test_the_published_port_always_names_a_bind_address() -> None:
    """A bare "1883:1883" is the defect, not a shorthand.

    Without a host address Docker publishes on every interface, and this broker
    has no TLS. The mapping must always say which interface it means.
    """
    compose = read(COMPOSE)
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"', compose, re.MULTILINE)

    host = published[0].rsplit(f":{MQTT_PORT}:{MQTT_PORT}", 1)[0]
    assert host, "the published port must carry an explicit host bind address"


def test_the_bind_address_defaults_to_loopback() -> None:
    """LAN exposure is opt-in, per capture, and never the default.

    An ESP8266 needs LAN reach to publish, so `POWERGUARD_MQTT_BIND` exists --
    but it takes one named host address, and leaving it unset keeps the broker
    on loopback.
    """
    compose = read(COMPOSE)
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"', compose, re.MULTILINE)

    host = published[0].rsplit(f":{MQTT_PORT}:{MQTT_PORT}", 1)[0]
    assert host == "${POWERGUARD_MQTT_BIND:-127.0.0.1}"
    # The default half of the substitution is what applies when nobody sets it.
    assert host.endswith(":-127.0.0.1}")


def test_lan_exposure_is_documented_with_its_assumptions() -> None:
    readme = read(BROKER_README)
    assert "POWERGUARD_MQTT_BIND" in readme
    # Exposing an unencrypted broker is a decision with conditions attached;
    # the document that enables it has to state them.
    assert "firewall" in readme.lower()
    assert "TLS" in readme


def test_no_websocket_listener_is_configured_or_claimed() -> None:
    """The broker serves MQTT only; no document may imply otherwise."""
    for conf in (NATIVE_CONF, DOCKER_CONF):
        assert "websockets" not in read(conf)
    assert "No WebSocket listener is configured" in read(BROKER_README)


# -- authentication and access control are actually on ---------------------


@pytest.mark.parametrize("conf", [NATIVE_CONF, DOCKER_CONF])
def test_anonymous_access_is_off_and_both_files_are_referenced(conf: Path) -> None:
    found = directives(conf)

    assert found["allow_anonymous"] == "false"
    assert found["password_file"].endswith("passwd")
    assert found["acl_file"].endswith("acl")


def test_the_native_config_uses_paths_that_resolve_from_the_backend_directory() -> None:
    """A native run uses the repository's own files, not container paths."""
    found = directives(NATIVE_CONF)

    for key in ("password_file", "acl_file"):
        value = found[key]
        assert not value.startswith("/mosquitto/"), f"{key} is a container path"
        assert (BACKEND / value).parent.is_dir(), f"{key} does not resolve"


def test_the_container_config_uses_the_paths_compose_mounts() -> None:
    found = directives(DOCKER_CONF)
    compose = read(COMPOSE)

    assert found["password_file"] == "/mosquitto/config/passwd"
    assert found["acl_file"] == "/mosquitto/config/acl"
    assert "/mosquitto/config/passwd:ro" in compose
    assert "/mosquitto/config/acl:ro" in compose
    assert "mosquitto.docker.conf:/mosquitto/config/mosquitto.conf:ro" in compose


def test_compose_starts_the_config_it_mounts() -> None:
    assert '"/mosquitto/config/mosquitto.conf"' in read(COMPOSE)


def test_container_persistence_has_somewhere_to_persist_to() -> None:
    found = directives(DOCKER_CONF)
    compose = read(COMPOSE)

    if found.get("persistence") == "true":
        location = found["persistence_location"].rstrip("/")
        assert f"{location}" in compose, "persistence without a mounted volume is lost"
        assert "volumes:" in compose


def test_a_native_run_leaves_nothing_behind() -> None:
    assert directives(NATIVE_CONF)["persistence"] == "false"


# -- the ACL matches the topics the system actually uses -------------------


def test_the_acl_confines_the_device_to_its_own_topics() -> None:
    acl = read(ACL)

    assert "user powerguard-device" in acl
    assert "topic write powerguard/v1/devices/powerguard-01/telemetry" in acl
    assert "topic write powerguard/v1/devices/powerguard-01/status" in acl
    assert "topic read" not in acl.split("user powerguard-backend")[0]


def test_the_acl_gives_the_backend_read_only_access_to_both_filters() -> None:
    backend_section = read(ACL).split("user powerguard-backend")[1]

    assert "topic read powerguard/v1/devices/+/telemetry" in backend_section
    assert "topic read powerguard/v1/devices/+/status" in backend_section
    assert "topic write" not in backend_section


# -- the instructions match the files --------------------------------------


def test_the_broker_readme_names_both_configurations() -> None:
    readme = read(BROKER_README)

    assert "mosquitto.native.conf" in readme
    assert "mosquitto.docker.conf" in readme
    assert "mosquitto.conf" not in readme.replace("mosquitto.native.conf", "").replace(
        "mosquitto.docker.conf", ""
    ).replace("/mosquitto/config/mosquitto.conf", ""), "a stale file name is quoted"


def test_the_native_command_uses_the_native_configuration() -> None:
    readme = read(BROKER_README)

    assert "mosquitto -c deploy\\mosquitto\\mosquitto.native.conf" in readme
    assert "mosquitto -c deploy\\mosquitto\\mosquitto.conf" not in readme


def test_the_readme_explains_the_password_file_before_using_it() -> None:
    readme = read(BROKER_README)
    create = readme.index("mosquitto_passwd")
    compose_up = readme.index("docker compose up")

    assert create < compose_up, "compose fails if the mounted password file is absent"
    assert "-c deploy\\mosquitto\\passwd powerguard-backend" in readme
    assert "powerguard-device" in readme


def test_the_readme_quotes_no_real_password_and_passes_none_in_argv() -> None:
    readme = read(BROKER_README)

    assert "<password>" in readme, "the placeholder, never a value"
    # A password on a command line is readable in any process listing.
    for flag in (" -P ", " --pw ", " -pw "):
        assert flag not in readme
    assert "check_broker.py" in readme, "the in-process check is what is offered"


@pytest.mark.parametrize("document", [BACKEND_README, OPERATIONS])
def test_the_backend_documents_point_at_the_real_broker_assets(document: Path) -> None:
    text = read(document)

    assert "deploy/mosquitto/README.md" in text
    # No document may claim a config file that does not exist.
    for name in re.findall(r"mosquitto\.[a-z.]*conf", text):
        assert (DEPLOY / name).exists(), f"{document.name} names a missing {name}"
