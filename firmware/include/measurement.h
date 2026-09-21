// AIoT PowerGuard - measurement value type (P02-T04).
//
// One acquisition attempt and its outcome. Deliberately separate from the
// sensor availability state: a READY sensor can still produce an invalid
// sample, and an invalid sample never overwrites the last valid one.
//
// Energy integration is intentionally outside the Phase 02 measurement scope.

#pragma once

#include <stdint.h>

namespace powerguard {
namespace measurement {

enum class MeasurementStatus : uint8_t {
  kOk,
  kSensorNotReady,      // sensor is not in READY state; no registers were read
  kTransportFailed,     // INA226 stopped acknowledging on the I2C bus
  kMathOverflow,        // INA226 flagged an internal arithmetic overflow
  kBusVoltageInvalid,   // non-finite bus voltage
  kBusVoltageOutOfRange,
  kCurrentInvalid,      // non-finite current
  kCurrentOutOfRange,
  kPowerInvalid,        // non-finite derived power
};

struct PowerMeasurement {
  uint32_t seq;          // monotonic per acquisition attempt, per boot
  uint32_t timestampMs;  // monotonic millis() taken at the acquisition attempt
  float voltageV;        // INA226 bus voltage
  float currentA;        // total system current; negative means reverse flow
  float powerW;          // signed: voltageV * currentA, negative on reverse flow
  bool valid;
  MeasurementStatus status;
};

MeasurementStatus validateElectricalValues(float voltageV,
                                            float currentA,
                                            float powerW,
                                            float maxBusVoltageV,
                                            float maxMeasurableCurrentA);

// Non-negative cumulative watt-hours for the current boot, integrated from
// consecutive valid samples using monotonic elapsed time.
//
// Integration continuity breaks on any invalid sample: the firmware did not
// measure that interval, so it must not be charged to the boot total.
class EnergyIntegrator {
 public:
  EnergyIntegrator();
  void reset();
  // Call for every acquisition attempt. Invalid samples break continuity.
  void update(const PowerMeasurement& sample);
  float energyWh() const { return energyWh_; }
  bool hasContinuity() const { return hasPrevious_; }
  uint32_t gapCount() const { return gapCount_; }

 private:
  float energyWh_;
  uint32_t lastSampleMs_;
  uint32_t gapCount_;
  bool hasPrevious_;
};

const char* describe(MeasurementStatus status);

}  // namespace measurement
}  // namespace powerguard
