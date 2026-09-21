"""WebSocket fan-out hub.

Each subscription owns a bounded queue. Ingestion never waits on a client: if a
consumer is too slow its queue overflows, and that one connection is dropped
with retryable close code 1013 while everyone else keeps receiving. Delivery is
best effort; REST over the database is the recovery path after a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from powerguard.domain.entities import Anomaly, Device, Telemetry
from powerguard.domain.ports import Clock
from powerguard.observability import log_event
from powerguard.realtime.events import Event, anomaly_event, status_event, telemetry_event

logger = logging.getLogger(__name__)

# Retryable close code: the client may reconnect immediately.
CLOSE_SLOW_CLIENT = 1013


class HubFullError(RuntimeError):
    """The process-wide connection cap was reached."""


# eq=False keeps identity hashing: two connections are never "the same"
# subscription just because their fields match.
@dataclass(slots=True, eq=False)
class Subscription:
    device_id: str
    queue: asyncio.Queue[Event]
    dropped: bool = False
    # Set when the hub decides this connection must end. Without it a dropped
    # slow client would stay parked on an empty-queue await and never actually
    # receive its close frame: its queue is full, so no sentinel could be
    # enqueued, and nothing else would ever wake the pump.
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self) -> None:
        self.closed.set()

    async def next_event(self) -> Event | None:
        """The next event, or None once this subscription has been closed."""
        if self.closed.is_set() and self.queue.empty():
            return None
        getter: asyncio.Task[Event] = asyncio.ensure_future(self.queue.get())
        waiter: asyncio.Task[bool] = asyncio.ensure_future(self.closed.wait())
        try:
            done, _pending = await asyncio.wait(
                {getter, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done:
                # An event that arrived in the same tick as the close is still
                # delivered: the close is observed on the following call.
                return getter.result()
            return None
        finally:
            # Both are cleaned up even when this coroutine is itself cancelled,
            # so no pending `Queue.get` survives the connection. `done()` is
            # checked first: cancelling a getter that already took an item
            # would drop that event.
            for task in (getter, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(getter, waiter, return_exceptions=True)


@dataclass(slots=True)
class HubCounters:
    opened: int = 0
    closed: int = 0
    rejected: int = 0
    slow_clients_dropped: int = 0
    events_published: int = 0
    events_delivered: int = 0


@dataclass
class EventHub:
    clock: Clock
    max_connections: int
    queue_size: int
    counters: HubCounters = field(default_factory=HubCounters)
    _subscriptions: dict[str, set[Subscription]] = field(default_factory=dict)

    # -- registration ------------------------------------------------------

    @property
    def connection_count(self) -> int:
        return sum(len(subs) for subs in self._subscriptions.values())

    def subscribe(self, device_id: str) -> Subscription:
        # The cap is enforced before registration, not after.
        if self.connection_count >= self.max_connections:
            self.counters.rejected += 1
            log_event(
                logger,
                logging.WARNING,
                "websocket_rejected",
                device_id=device_id,
                reason="connection cap",
                cap=self.max_connections,
            )
            raise HubFullError("websocket connection cap reached")
        subscription = Subscription(
            device_id=device_id, queue=asyncio.Queue(maxsize=self.queue_size)
        )
        self._subscriptions.setdefault(device_id, set()).add(subscription)
        self.counters.opened += 1
        log_event(logger, logging.INFO, "websocket_opened", device_id=device_id)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Always called from a finally block, so a slot is never leaked."""
        subscription.close()
        subscribers = self._subscriptions.get(subscription.device_id)
        if subscribers is None:
            return
        if subscription not in subscribers:
            # Already unregistered by the overflow path; counted exactly once.
            return
        subscribers.discard(subscription)
        if not subscribers:
            del self._subscriptions[subscription.device_id]
        self.counters.closed += 1
        log_event(
            logger, logging.INFO, "websocket_closed", device_id=subscription.device_id
        )

    # -- publishing --------------------------------------------------------

    def _dispatch(self, event: Event) -> None:
        self.counters.events_published += 1
        subscribers = self._subscriptions.get(event.device_id)
        if not subscribers:
            return
        for subscription in list(subscribers):
            try:
                subscription.queue.put_nowait(event)
                self.counters.events_delivered += 1
            except asyncio.QueueFull:
                # Mark and unregister only this client; never block ingestion.
                subscription.dropped = True
                # Wake the pump so the connection really closes with 1013
                # instead of waiting for an event that will never arrive.
                subscription.close()
                self.counters.slow_clients_dropped += 1
                self.counters.closed += 1
                subscribers.discard(subscription)
                log_event(
                    logger,
                    logging.WARNING,
                    "websocket_slow_client_dropped",
                    device_id=event.device_id,
                    queue_size=self.queue_size,
                )
        if not subscribers:
            self._subscriptions.pop(event.device_id, None)

    async def publish_telemetry(self, telemetry: Telemetry) -> None:
        self._dispatch(telemetry_event(telemetry, self.clock.now()))

    async def publish_anomaly(self, anomaly: Anomaly) -> None:
        self._dispatch(anomaly_event(anomaly, self.clock.now()))

    async def publish_device_status(self, device: Device) -> None:
        self._dispatch(status_event(device, self.clock.now()))
