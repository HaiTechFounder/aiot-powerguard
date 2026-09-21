"""A deterministic fixture, for verifying software -- not for measuring accuracy.

Every number here was invented. It exercises the pipeline's mechanics: that a
window is built, a model fits, an artifact round-trips, an adapter answers. It
says nothing whatsoever about how the detector behaves on a real load, and any
metric computed on it is a statement about this generator.

The seed is fixed so a rerun produces byte-identical data, which is what makes
"deterministic rerun produces equivalent decisions" a testable claim.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from powerguard_ml.dataset import Sample

DEFAULT_SEED = 42
DEFAULT_DEVICE = "pg-synthetic-01"
CALIBRATION_FINGERPRINT = "synthetic-regime-v1"

#: A plausible-looking operating point. Plausible is not measured.
NOMINAL_VOLTAGE_V = 7.84
NOMINAL_CURRENT_A = 0.42
EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class Scenario:
    """An injected departure from the nominal signal, and where it sits."""

    kind: str
    start: int
    length: int

    @property
    def stop(self) -> int:
        return self.start + self.length

    def covers(self, index: int) -> bool:
        return self.start <= index < self.stop


def generate(
    count: int,
    *,
    seed: int = DEFAULT_SEED,
    device_id: str = DEFAULT_DEVICE,
    boot_id: str = "boot-0001",
    start_id: int = 1,
    start_seq: int = 1,
    start_at: dt.datetime = EPOCH,
    cadence_seconds: float = 2.0,
    scenarios: tuple[Scenario, ...] = (),
    noise_v: float = 0.01,
    noise_a: float = 0.004,
) -> list[Sample]:
    """`count` contiguous rows, with any injected scenarios applied.

    Rows are labelled 1 inside a scenario and 0 outside it. That label is a
    property of this generator, not an operator's judgement, and it is only
    ever used to check that the pipeline reacts to an injected departure.
    """
    rng = np.random.default_rng(seed)
    voltage = NOMINAL_VOLTAGE_V + rng.normal(0.0, noise_v, count)
    current = NOMINAL_CURRENT_A + rng.normal(0.0, noise_a, count)

    for scenario in scenarios:
        stop = min(scenario.stop, count)
        if scenario.start >= stop:
            continue
        span = slice(scenario.start, stop)
        length = stop - scenario.start
        if scenario.kind == "spike":
            current[span] += 2.4
            voltage[span] -= 0.5
        elif scenario.kind == "level_shift":
            current[span] += 0.9
        elif scenario.kind == "drift":
            current[span] += np.linspace(0.0, 1.1, length)
        else:
            raise ValueError(f"unknown scenario kind: {scenario.kind!r}")

    samples: list[Sample] = []
    energy = 0.0
    for index in range(count):
        power = float(voltage[index] * current[index])
        energy += power * cadence_seconds / 3600.0
        labelled = any(scenario.covers(index) for scenario in scenarios)
        samples.append(
            Sample(
                id=start_id + index,
                device_id=device_id,
                boot_id=boot_id,
                seq=start_seq + index,
                received_at=start_at + dt.timedelta(seconds=cadence_seconds * index),
                voltage_v=round(float(voltage[index]), 4),
                current_a=round(float(current[index]), 4),
                power_w=round(power, 4),
                energy_wh=round(energy, 6),
                sensor_status="ok",
                label=1 if labelled else 0,
                calibration_fingerprint=CALIBRATION_FINGERPRINT,
            )
        )
    return samples


def training_set(count: int = 900, *, seed: int = DEFAULT_SEED) -> list[Sample]:
    """Nominal rows only: what a baseline is supposed to be fitted on."""
    return generate(count, seed=seed)


def evaluation_set(
    count: int = 600, *, seed: int = DEFAULT_SEED + 1
) -> tuple[list[Sample], tuple[Scenario, ...]]:
    """Nominal rows with one of each injected scenario, well separated."""
    scenarios = (
        Scenario("spike", start=150, length=20),
        Scenario("level_shift", start=300, length=40),
        Scenario("drift", start=450, length=60),
    )
    return (
        generate(
            count,
            seed=seed,
            boot_id="boot-0002",
            start_id=10_001,
            scenarios=scenarios,
        ),
        scenarios,
    )
