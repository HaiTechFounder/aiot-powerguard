// AIoT PowerGuard - measurement value type helpers.

#include "measurement.h"

#include <math.h>

namespace powerguard {
namespace measurement {

MeasurementStatus validateElectricalValues(const float voltageV,
                                            const float currentA,
                                            const float powerW,
                                            const float maxBusVoltageV,
                                            const float maxMeasurableCurrentA) {
  if (!isfinite(voltageV)) {
    return MeasurementStatus::kBusVoltageInvalid;
  }
  if (!isfinite(maxBusVoltageV) || maxBusVoltageV <= 0.0f ||
      voltageV < 0.0f || voltageV > maxBusVoltageV) {
    return MeasurementStatus::kBusVoltageOutOfRange;
  }
  if (!isfinite(currentA)) {
    return MeasurementStatus::kCurrentInvalid;
  }
  if (!isfinite(maxMeasurableCurrentA) || maxMeasurableCurrentA <= 0.0f ||
      fabsf(currentA) > maxMeasurableCurrentA) {
    return MeasurementStatus::kCurrentOutOfRange;
  }
  if (!isfinite(powerW)) {
    return MeasurementStatus::kPowerInvalid;
  }
  return MeasurementStatus::kOk;
}

EnergyIntegrator::EnergyIntegrator()
    : energyWh_(0.0f), lastSampleMs_(0), gapCount_(0), hasPrevious_(false) {}

void EnergyIntegrator::reset() {
  energyWh_ = 0.0f;
  lastSampleMs_ = 0;
  gapCount_ = 0;
  hasPrevious_ = false;
}

void EnergyIntegrator::update(const PowerMeasurement& sample) {
  if (!sample.valid || sample.status != MeasurementStatus::kOk) {
    // Unmeasured interval: drop continuity so the gap is never integrated.
    if (hasPrevious_) {
      ++gapCount_;
    }
    hasPrevious_ = false;
    return;
  }
  if (!hasPrevious_) {
    hasPrevious_ = true;
    lastSampleMs_ = sample.timestampMs;
    return;
  }

  // Rollover-safe monotonic elapsed time.
  const uint32_t elapsedMs = sample.timestampMs - lastSampleMs_;
  lastSampleMs_ = sample.timestampMs;
  if (elapsedMs == 0) {
    return;
  }

  const float deltaWh = sample.powerW * (static_cast<float>(elapsedMs) / 3600000.0f);
  const float updated = energyWh_ + deltaWh;
  if (!isfinite(updated)) {
    return;  // keep the last good total rather than poisoning it
  }
  // MQTT_SPEC requires a non-negative cumulative value; reverse flow must not
  // drive the boot total below zero.
  energyWh_ = updated < 0.0f ? 0.0f : updated;
}

const char* describe(MeasurementStatus status) {
  switch (status) {
    case MeasurementStatus::kOk:
      return "ok";
    case MeasurementStatus::kSensorNotReady:
      return "sensor not ready";
    case MeasurementStatus::kTransportFailed:
      return "INA226 stopped responding on I2C";
    case MeasurementStatus::kMathOverflow:
      return "INA226 arithmetic overflow";
    case MeasurementStatus::kBusVoltageInvalid:
      return "bus voltage is not a finite number";
    case MeasurementStatus::kBusVoltageOutOfRange:
      return "bus voltage is outside the configured range";
    case MeasurementStatus::kCurrentInvalid:
      return "current is not a finite number";
    case MeasurementStatus::kCurrentOutOfRange:
      return "current exceeds the INA226 measurement range";
    case MeasurementStatus::kPowerInvalid:
      return "derived power is not a finite number";
  }
  return "unknown measurement status";
}

}  // namespace measurement
}  // namespace powerguard
