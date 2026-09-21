"""MQTT v1 topic parsing.

Segment-based, never a permissive regex search over the whole string: a topic
is accepted only when it has exactly five segments in the expected shape. The
payload carries no device id, so the topic value is authoritative (MQTT_SPEC).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

NAMESPACE = ("powerguard", "v1", "devices")
DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

TELEMETRY_FILTER = "powerguard/v1/devices/+/telemetry"
STATUS_FILTER = "powerguard/v1/devices/+/status"
SUBSCRIPTIONS = (TELEMETRY_FILTER, STATUS_FILTER)


class TopicKind(StrEnum):
    TELEMETRY = "telemetry"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class ParsedTopic:
    device_id: str
    kind: TopicKind


def is_valid_device_id(value: str) -> bool:
    return bool(DEVICE_ID_PATTERN.match(value))


def parse_topic(topic: str) -> ParsedTopic | None:
    """Return the parsed topic, or None when it is not a v1 device topic."""
    segments = topic.split("/")
    if len(segments) != 5:
        return None
    if tuple(segments[:3]) != NAMESPACE:
        return None
    device_id = segments[3]
    if not is_valid_device_id(device_id):
        return None
    try:
        kind = TopicKind(segments[4])
    except ValueError:
        return None
    return ParsedTopic(device_id=device_id, kind=kind)


def telemetry_topic(device_id: str) -> str:
    return f"powerguard/v1/devices/{device_id}/telemetry"


def status_topic(device_id: str) -> str:
    return f"powerguard/v1/devices/{device_id}/status"
