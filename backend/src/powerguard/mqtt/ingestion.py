"""Ingestion use case.

Implements the outcome matrix in BACKEND_SPEC section 9:

| Outcome                      | Persist | Broadcast | Ack |
|------------------------------|---------|-----------|-----|
| Accepted telemetry           | yes     | after commit | yes |
| Duplicate telemetry          | no      | no        | yes |
| Accepted status              | yes     | after commit | yes |
| Deterministically invalid    | no      | no        | yes (no poison loop) |
| Transient storage failure    | no      | no        | no (redeliver)      |

Blocking SQLAlchemy work runs in a worker thread, with the session created and
closed entirely inside that thread.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

from powerguard.config import Settings
from powerguard.device_status import DeviceStatusTracker
from powerguard.domain.entities import (
    Anomaly,
    Device,
    DeviceStatus,
    Duplicate,
    Telemetry,
)
from powerguard.domain.errors import TransientStorageError
from powerguard.domain.ports import Clock, EventPublisher, InferenceEngine, UnitOfWorkFactory
from powerguard.mqtt.ports import AckDecision, InboundMessage
from powerguard.mqtt.validation import (
    Invalid,
    RejectionReason,
    ValidStatus,
    ValidTelemetry,
    validate_message,
)
from powerguard.observability import log_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestionCounters:
    """The ingest, status and inference categories required by BACKEND_SPEC 13."""

    accepted_telemetry: int = 0
    duplicate_telemetry: int = 0
    accepted_status: int = 0
    rejected: int = 0
    transient_failures: int = 0
    inference_unavailable: int = 0
    inference_failures: int = 0
    anomalies_detected: int = 0
    sequence_gaps: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: RejectionReason) -> None:
        self.rejected += 1
        self.rejections_by_reason[reason.value] = (
            self.rejections_by_reason.get(reason.value, 0) + 1
        )


@dataclass(frozen=True, slots=True)
class IngestionResult:
    ack: AckDecision
    outcome: str
    telemetry_id: int | None = None
    anomaly_id: int | None = None
    reason: RejectionReason | None = None


class IngestionService:
    def __init__(
        self,
        *,
        settings: Settings,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        inference: InferenceEngine,
        publisher: EventPublisher,
        status: DeviceStatusTracker | None = None,
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory
        self._clock = clock
        self._inference = inference
        self._publisher = publisher
        # Shared with the stale scan, so every transition has one origin.
        self.status = status if status is not None else DeviceStatusTracker()
        self.counters = IngestionCounters()
        # Highest sequence seen per (device, boot) - diagnostics only. Gaps are
        # counted, never backfilled, and never affect idempotency.
        self._last_seq: dict[tuple[str, str], int] = {}

    # -- entry point -------------------------------------------------------

    async def handle(self, message: InboundMessage) -> IngestionResult:
        result = validate_message(
            message.topic,
            message.payload,
            self._settings,
            retained=message.retained,
        )

        if isinstance(result, Invalid):
            self.counters.reject(result.reason)
            # Deterministically invalid: acknowledging prevents a poison loop.
            # `detail` is a field name or a bound, never payload content.
            log_event(
                logger,
                logging.WARNING,
                "ingest_rejected",
                topic=message.topic,
                reason=result.reason.value,
                detail=result.detail,
            )
            return IngestionResult(
                ack=AckDecision.ACK, outcome="rejected", reason=result.reason
            )

        try:
            if isinstance(result, ValidTelemetry):
                return await self._handle_telemetry(result, message.received_at)
            return await self._handle_status(result, message.received_at)
        except TransientStorageError:
            self.counters.transient_failures += 1
            log_event(
                logger, logging.ERROR, "ingest_storage_failure", topic=message.topic
            )
            logger.exception("ingest_storage_failure_detail topic=%s", message.topic)
            # Never acknowledged: the broker must redeliver.
            return IngestionResult(ack=AckDecision.WITHHOLD, outcome="transient_failure")

    # -- telemetry ---------------------------------------------------------

    async def _handle_telemetry(
        self, valid: ValidTelemetry, received_at: dt.datetime
    ) -> IngestionResult:
        payload = valid.payload
        telemetry = Telemetry(
            device_id=valid.topic.device_id,
            boot_id=payload.boot_id,
            seq=payload.seq,
            received_at=received_at,
            sampled_at=payload.sampled_at_datetime(),
            voltage_v=payload.voltage_v,
            current_a=payload.current_a,
            power_w=payload.power_w,
            energy_wh=payload.energy_wh,
        )

        stored = await asyncio.to_thread(
            self._persist_telemetry, telemetry, payload.firmware_version
        )

        if isinstance(stored, Duplicate):
            self.counters.duplicate_telemetry += 1
            log_event(
                logger,
                logging.INFO,
                "ingest_duplicate",
                device_id=valid.topic.device_id,
                boot_id=payload.boot_id,
                seq=payload.seq,
                telemetry_id=stored.telemetry_id,
            )
            # A duplicate produces no inference and no event.
            return IngestionResult(
                ack=AckDecision.ACK, outcome="duplicate", telemetry_id=stored.telemetry_id
            )

        self.counters.accepted_telemetry += 1
        # Telemetry proves the device is active. The repository has already
        # moved the row to online, so a stale or offline device recovering here
        # is a real transition and must be reported like any other.
        self.status.record(
            stored.device_id,
            DeviceStatus.ONLINE,
            source="telemetry",
            boot_id=stored.boot_id,
        )
        log_event(
            logger,
            logging.INFO,
            "ingest_accepted",
            device_id=stored.device_id,
            boot_id=stored.boot_id,
            seq=stored.seq,
            telemetry_id=stored.id,
        )
        self._note_sequence(stored)

        anomaly = await self._maybe_record_anomaly(stored)

        # Only committed records are broadcast, telemetry before anomaly. The
        # recovery above is an observability transition, not a new frame: the
        # v1 per-reading order stays exactly as API_CONTRACT defines it.
        await self._publisher.publish_telemetry(stored)
        if anomaly is not None:
            await self._publisher.publish_anomaly(anomaly)

        return IngestionResult(
            ack=AckDecision.ACK,
            outcome="accepted",
            telemetry_id=stored.id,
            anomaly_id=anomaly.id if anomaly else None,
        )

    def _persist_telemetry(
        self, telemetry: Telemetry, firmware_version: str
    ) -> Telemetry | Duplicate:
        """One short transaction: device upsert plus telemetry insert."""
        with self._uow_factory() as uow:
            uow.devices.upsert_from_telemetry(
                telemetry.device_id,
                firmware_version,
                telemetry.boot_id,
                telemetry.received_at,
            )
            stored = uow.telemetry.insert_or_resolve_duplicate(telemetry)
            uow.commit()
            return stored

    async def _maybe_record_anomaly(self, telemetry: Telemetry) -> Anomaly | None:
        """Inference runs after the telemetry commit and is failure-isolated."""
        try:
            verdict = self._inference.evaluate(telemetry)
        except Exception:
            self.counters.inference_failures += 1
            log_event(
                logger,
                logging.ERROR,
                "inference_failed",
                device_id=telemetry.device_id,
                telemetry_id=telemetry.id,
            )
            logger.exception("inference_failed_detail device=%s", telemetry.device_id)
            return None
        if verdict is None:
            if self._inference.readiness() != "ready":
                self.counters.inference_unavailable += 1
            return None
        if telemetry.id is None:  # pragma: no cover - persisted rows have an id
            return None

        candidate = Anomaly(
            telemetry_id=telemetry.id,
            device_id=telemetry.device_id,
            detected_at=self._clock.now(),
            method=verdict.method,
            reasons=verdict.reasons,
            score=verdict.score,
            model_version=verdict.model_version,
        )
        try:
            stored = await asyncio.to_thread(self._persist_anomaly, candidate)
        except TransientStorageError:
            # The telemetry is already durable; only the verdict is lost.
            self.counters.transient_failures += 1
            log_event(
                logger,
                logging.ERROR,
                "anomaly_storage_failure",
                device_id=telemetry.device_id,
                telemetry_id=telemetry.id,
            )
            return None
        self.counters.anomalies_detected += 1
        log_event(
            logger,
            logging.WARNING,
            "anomaly_detected",
            device_id=stored.device_id,
            telemetry_id=stored.telemetry_id,
            anomaly_id=stored.id,
            method=stored.method.value,
        )
        return stored

    def _persist_anomaly(self, anomaly: Anomaly) -> Anomaly:
        """Second transaction, tied to the already committed telemetry row."""
        with self._uow_factory() as uow:
            stored = uow.anomalies.insert(anomaly)
            uow.commit()
            return stored

    def _note_sequence(self, telemetry: Telemetry) -> None:
        key = (telemetry.device_id, telemetry.boot_id)
        previous = self._last_seq.get(key)
        if previous is not None and telemetry.seq > previous + 1:
            self.counters.sequence_gaps += 1
            log_event(
                logger,
                logging.INFO,
                "ingest_sequence_gap",
                device_id=telemetry.device_id,
                boot_id=telemetry.boot_id,
                from_seq=previous,
                to_seq=telemetry.seq,
            )
        if previous is None or telemetry.seq > previous:
            self._last_seq[key] = telemetry.seq

    # -- status ------------------------------------------------------------

    async def _handle_status(
        self, valid: ValidStatus, received_at: dt.datetime
    ) -> IngestionResult:
        payload = valid.payload
        device = await asyncio.to_thread(
            self._persist_status,
            valid.topic.device_id,
            payload.firmware_version,
            payload.boot_id,
            DeviceStatus(payload.status),
            received_at,
        )
        self.counters.accepted_status += 1
        self.status.record(
            device.id,
            device.status,
            source="status_message",
            boot_id=payload.boot_id,
            retained=valid.retained,
        )
        await self._publisher.publish_device_status(device)
        return IngestionResult(ack=AckDecision.ACK, outcome="status")

    def _persist_status(
        self,
        device_id: str,
        firmware_version: str,
        boot_id: str,
        status: DeviceStatus,
        seen_at: dt.datetime,
    ) -> Device:
        with self._uow_factory() as uow:
            device = uow.devices.upsert_from_status(
                device_id, firmware_version, boot_id, status, seen_at
            )
            uow.commit()
            return device
