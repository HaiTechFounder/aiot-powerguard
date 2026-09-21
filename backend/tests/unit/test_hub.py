"""WebSocket fan-out hub: bounds, isolation and event shapes."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from powerguard.domain.entities import AnomalyMethod, Device, DeviceStatus
from powerguard.domain.services import FixedClock
from powerguard.realtime.hub import EventHub, HubFullError
from tests import builders
from tests.builders import DEVICE_ID
from tests.conftest import T0


@pytest.fixture
def hub(clock: FixedClock) -> EventHub:
    return EventHub(clock=clock, max_connections=3, queue_size=2)


async def test_telemetry_event_envelope_matches_v1(hub: EventHub, clock: FixedClock) -> None:
    subscription = hub.subscribe(DEVICE_ID)
    await hub.publish_telemetry(builders.telemetry(seq=7, received_at=clock.now()))

    envelope = (await subscription.next_event()).envelope()
    assert envelope["schema_version"] == 1
    assert envelope["type"] == "telemetry"
    assert envelope["emitted_at"].endswith("Z")
    assert len(envelope["emitted_at"]) == len("2026-09-21T03:00:00.000Z")
    data = envelope["data"]
    assert data["device_id"] == DEVICE_ID
    assert data["seq"] == 7
    assert data["sensor_status"] == "ok"
    assert data["sampled_at"] is None


async def test_anomaly_and_status_envelopes(hub: EventHub, clock: FixedClock) -> None:
    subscription = hub.subscribe(DEVICE_ID)
    await hub.publish_anomaly(
        builders.anomaly(telemetry_id=5, detected_at=clock.now(), method=AnomalyMethod.HYBRID)
    )
    device = Device(
        id=DEVICE_ID,
        firmware_version="0.1.0",
        status=DeviceStatus.STALE,
        first_seen_at=T0,
        last_seen_at=T0 + dt.timedelta(seconds=30),
    )
    await hub.publish_device_status(device)

    anomaly = (await subscription.next_event()).envelope()
    assert anomaly["type"] == "anomaly"
    assert anomaly["data"]["method"] == "hybrid"
    assert anomaly["data"]["telemetry_id"] == 5

    status = (await subscription.next_event()).envelope()
    assert status["type"] == "status"
    # The status frame carries exactly the approved fields.
    assert set(status["data"]) == {"device_id", "status", "last_seen_at"}
    assert status["data"]["status"] == "stale"


async def test_events_only_reach_subscribers_of_that_device(
    hub: EventHub, clock: FixedClock
) -> None:
    mine = hub.subscribe(DEVICE_ID)
    other = hub.subscribe("powerguard-02")
    await hub.publish_telemetry(builders.telemetry(received_at=clock.now()))

    assert mine.queue.qsize() == 1
    assert other.queue.qsize() == 0


def test_connection_cap_is_enforced_before_registration(hub: EventHub) -> None:
    for _ in range(3):
        hub.subscribe(DEVICE_ID)
    with pytest.raises(HubFullError):
        hub.subscribe(DEVICE_ID)
    assert hub.counters.rejected == 1
    assert hub.connection_count == 3


def test_unsubscribe_frees_a_slot(hub: EventHub) -> None:
    subscriptions = [hub.subscribe(DEVICE_ID) for _ in range(3)]
    hub.unsubscribe(subscriptions[0])
    assert hub.connection_count == 2
    hub.subscribe(DEVICE_ID)  # the freed slot is reusable
    assert hub.counters.closed == 1


async def test_slow_client_is_dropped_without_blocking_others(
    hub: EventHub, clock: FixedClock
) -> None:
    slow = hub.subscribe(DEVICE_ID)
    fast = hub.subscribe(DEVICE_ID)

    # Fill both queues to capacity (2), then overflow.
    for seq in range(3):
        await hub.publish_telemetry(builders.telemetry(seq=seq, received_at=clock.now()))
        # The fast client keeps up after the first two.
        if seq < 2:
            await fast.next_event()

    assert slow.dropped is True
    assert hub.counters.slow_clients_dropped >= 1
    # The healthy connection is untouched and still registered.
    assert fast.dropped is False


async def test_publishing_with_no_subscribers_is_harmless(
    hub: EventHub, clock: FixedClock
) -> None:
    await hub.publish_telemetry(builders.telemetry(received_at=clock.now()))
    assert hub.counters.events_published == 1
    assert hub.counters.events_delivered == 0


async def test_dropping_a_slow_client_wakes_its_pump(
    hub: EventHub, clock: FixedClock
) -> None:
    """The close must be observable, not merely recorded.

    A dropped subscription has a full queue, so no sentinel event could ever be
    enqueued. Without an explicit close signal the connection would sit on an
    await that never completes and would never send close 1013.
    """
    slow = hub.subscribe(DEVICE_ID)

    for seq in range(3):  # queue size is 2
        await hub.publish_telemetry(builders.telemetry(seq=seq, received_at=clock.now()))

    assert slow.dropped is True
    assert slow.closed.is_set()

    # Buffered events are still delivered, and then the close is reported.
    assert await asyncio.wait_for(slow.next_event(), timeout=1) is not None
    assert await asyncio.wait_for(slow.next_event(), timeout=1) is not None
    assert await asyncio.wait_for(slow.next_event(), timeout=1) is None


async def test_a_waiting_pump_is_released_by_unsubscribe(hub: EventHub) -> None:
    """An idle connection being closed must not leave its pump parked."""
    subscription = hub.subscribe(DEVICE_ID)
    waiting = asyncio.ensure_future(subscription.next_event())
    await asyncio.sleep(0)
    assert not waiting.done()

    hub.unsubscribe(subscription)

    assert await asyncio.wait_for(waiting, timeout=1) is None


async def test_a_dropped_client_is_counted_as_closed_exactly_once(
    hub: EventHub, clock: FixedClock
) -> None:
    slow = hub.subscribe(DEVICE_ID)
    for seq in range(3):
        await hub.publish_telemetry(builders.telemetry(seq=seq, received_at=clock.now()))
    assert hub.counters.closed == 1

    # The endpoint's finally block always unsubscribes; that must not count twice.
    hub.unsubscribe(slow)
    assert hub.counters.closed == 1
    assert hub.connection_count == 0
