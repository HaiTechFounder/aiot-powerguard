"""Scoring a live stream without acting on it.

Shadow mode is how a model earns trust: it sees real traffic, its verdicts are
counted and logged, and none of them become an anomaly row, a WebSocket frame
or anything an operator is shown as a finding. The wrapped engine is driven
exactly as it would be in production, so what shadow measures is what
activation would do.

Readiness stays ``unavailable`` on purpose. The health contract has two values
and ``ready`` is the one the dashboard renders as "detection is running" --
which, while every verdict is being discarded, would be false. Shadow activity
is visible in the structured log and in these counters, not in a status field
that would overstate it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from powerguard.domain.entities import AnomalyVerdict, Telemetry
from powerguard.domain.ports import InferenceEngine
from powerguard.observability import log_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ShadowCounters:
    """What the shadow run has seen. Evidence, not a validation result."""

    evaluated: int = 0
    would_flag: int = 0
    errors: int = 0


class ShadowInference:
    """Drive a real engine, discard its verdicts, keep the tally."""

    def __init__(self, engine: InferenceEngine, *, counters: ShadowCounters | None = None) -> None:
        self._engine = engine
        self.counters = counters if counters is not None else ShadowCounters()

    def readiness(self) -> str:
        # Never `ready`: nothing this engine says reaches the system.
        return "unavailable"

    def evaluate(self, telemetry: Telemetry) -> AnomalyVerdict | None:
        self.counters.evaluated += 1
        try:
            verdict = self._engine.evaluate(telemetry)
        except Exception:
            # A shadow model must not be able to break ingestion. Its whole
            # purpose is to be observed while it cannot do harm.
            self.counters.errors += 1
            logger.exception("shadow inference raised; telemetry is unaffected")
            return None

        if verdict is not None:
            self.counters.would_flag += 1
            log_event(
                logger,
                logging.INFO,
                "inference.shadow.would_flag",
                device_id=telemetry.device_id,
                seq=telemetry.seq,
                score=verdict.score,
                method=str(verdict.method),
                evaluated=self.counters.evaluated,
                would_flag=self.counters.would_flag,
            )
        # Discarded deliberately: an experimental finding is not an alert.
        return None
