"""Inference adapter used until a trained model exists.

Reports ``unavailable`` and never produces a verdict. Telemetry ingestion must
continue normally; only the anomaly step is skipped.
"""

from __future__ import annotations

from powerguard.domain.entities import AnomalyVerdict, Telemetry


class UnavailableInference:
    def readiness(self) -> str:
        return "unavailable"

    def evaluate(self, telemetry: Telemetry) -> AnomalyVerdict | None:
        del telemetry
        return None
