// AIoT PowerGuard - INA226 bring-up and presence detection (P02-T03).
//
// Owns the I2C bus setup, INA226 calibration and the sensor availability state
// machine. Measurement acquisition arrives in P02-T04; this module only proves
// the device is present, identified and calibrated.

#pragma once

#include <INA226.h>
#include <stdint.h>

#include "backoff.h"
#include "config.h"
#include "measurement.h"

class Print;

namespace powerguard {
namespace sensor {

// Availability of the INA226 as seen by the firmware.
enum class SensorState : uint8_t {
  kNotInitialized,  // begin() has not run yet
  kConfigError,     // acquisition configuration cannot produce a valid calibration
  kNotFound,        // no I2C device answered at the configured address
  kInitFailed,      // device answered but identification or calibration failed
  kReady,           // detected, identified and calibrated
};

// Why the last bring-up attempt did not reach kReady.
enum class SensorError : uint8_t {
  kNone,
  kShuntInvalid,
  kMaxCurrentMissing,       // HARDWARE_CONFIGURATION_REQUIRED
  kMaxCurrentUnmeasurable,  // maxCurrent * shunt exceeds INA226 shunt full scale
  kAddressInvalid,
  kPinsInvalid,
  kNoI2cResponse,
  kIdentityMismatch,
  kCalibrationRejected,  // driver refused setMaxCurrentShunt()
  kNotCalibrated,        // driver accepted but current LSB stayed zero
  kModeSetFailed,
};

// Retry policy for a missing or failing sensor: non-blocking, doubling, capped,
// with jitter applied on top of the base window (P02-T10 shared policy).
constexpr uint32_t kRetryInitialMs = 1000UL;
constexpr uint32_t kRetryMaxMs = 60000UL;

class PowerSensor {
 public:
  explicit PowerSensor(const config::Config& cfg);

  // Configures I2C, detects the INA226 and applies calibration derived from the
  // configured shunt and MAX_EXPECTED_CURRENT_A. Returns true when kReady.
  bool begin();

  // Non-blocking retry driver. Call from loop(); re-attempts begin() on the
  // capped backoff schedule while the sensor is not ready. Never blocks or
  // reboots. Returns true when a retry ran on this call.
  bool tick(uint32_t nowMs);

  // Live I2C probe. Detection only - reads no measurement registers.
  bool isResponding();

  // One acquisition attempt: bus voltage, total current and sensor-reported
  // power. Always consumes a sequence number, even when the attempt fails, so
  // dropped samples stay visible downstream. Never blocks and never reboots.
  // A transport failure demotes the sensor back to the retry state.
  measurement::PowerMeasurement read();

  // Last acquisition that passed validation. Kept separate from sensor state;
  // a failed read never overwrites it and callers must not treat it as a new
  // reading (FIRMWARE_SPEC sampling rule 6).
  bool hasLastValid() const { return hasLastValid_; }
  const measurement::PowerMeasurement& lastValid() const { return lastValid_; }

  // Attempts made so far this boot (equals the last issued sequence number).
  uint32_t sequence() const { return seq_; }

  SensorState state() const { return state_; }
  SensorError error() const { return error_; }
  bool isReady() const { return state_ == SensorState::kReady; }

  // Raw driver return code from setMaxCurrentShunt(), for diagnostics.
  int driverError() const { return driverError_; }

  uint16_t manufacturerId() const { return manufacturerId_; }
  uint16_t dieId() const { return dieId_; }

  // Calibration actually applied by the driver (0 until kReady).
  float currentLsbA() const { return currentLsbA_; }

  uint32_t attemptCount() const { return attemptCount_; }
  uint32_t retryDelayMs() const { return backoff_.delayMs(); }

  // One-line bring-up report. Contains no secrets.
  void printStatus(Print& out) const;

 private:
  SensorError validateAcquisitionConfig() const;
  void fail(SensorState state, SensorError error);
  // Marks nowMs as the start of the current backoff window.
  void armRetry(uint32_t nowMs);

  const config::Config& cfg_;
  INA226 device_;

  SensorState state_;
  SensorError error_;
  int driverError_;
  uint16_t manufacturerId_;
  uint16_t dieId_;
  float currentLsbA_;

  uint32_t attemptCount_;
  net::Backoff backoff_;

  uint32_t seq_;
  measurement::PowerMeasurement lastValid_;
  bool hasLastValid_;
};

const char* describe(SensorState state);
const char* describe(SensorError error);

}  // namespace sensor
}  // namespace powerguard
