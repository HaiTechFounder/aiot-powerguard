// AIoT PowerGuard - INA226 bring-up and presence detection.

#include "power_sensor.h"

#include <Arduino.h>
#include <Wire.h>
#include <math.h>

namespace powerguard {
namespace sensor {
namespace {

bool isPositiveFinite(float value) { return isfinite(value) && value > 0.0f; }

bool isUsablePin(uint8_t pin) { return pin <= 16; }

}  // namespace

PowerSensor::PowerSensor(const config::Config& cfg)
    : cfg_(cfg),
      device_(cfg.i2c.sensorAddress),
      state_(SensorState::kNotInitialized),
      error_(SensorError::kNone),
      driverError_(INA226_ERR_NONE),
      manufacturerId_(0),
      dieId_(0),
      currentLsbA_(0.0f),
      attemptCount_(0),
      backoff_(kRetryInitialMs, kRetryMaxMs, POWERGUARD_BACKOFF_JITTER_PERCENT),
      seq_(0),
      lastValid_(),
      hasLastValid_(false) {}

SensorError PowerSensor::validateAcquisitionConfig() const {
  const config::SensorConfig& sensor = cfg_.sensor;
  const config::I2cConfig& i2c = cfg_.i2c;

  if (!isUsablePin(i2c.sclPin) || !isUsablePin(i2c.sdaPin) || i2c.sclPin == i2c.sdaPin) {
    return SensorError::kPinsInvalid;
  }
  if (i2c.sensorAddress < 0x40 || i2c.sensorAddress > 0x4F) {
    return SensorError::kAddressInvalid;
  }
  if (!isPositiveFinite(sensor.shuntResistanceOhm)) {
    return SensorError::kShuntInvalid;
  }
  // No guessed fallback: an unset MAX_EXPECTED_CURRENT_A keeps the sensor down.
  if (!isPositiveFinite(sensor.maxExpectedCurrentA)) {
    return SensorError::kMaxCurrentMissing;
  }
  if (sensor.maxExpectedCurrentA > config::maxMeasurableCurrentA(sensor)) {
    return SensorError::kMaxCurrentUnmeasurable;
  }
  return SensorError::kNone;
}

void PowerSensor::fail(SensorState state, SensorError error) {
  state_ = state;
  error_ = error;
  currentLsbA_ = 0.0f;

  // Arm the backoff window from the moment of failure, including the very first
  // failure raised by begin() in setup(). Without this the first tick() would
  // retry immediately and the required 1 s step would be skipped.
  // A configuration error is not retryable; it needs operator action.
  if (state == SensorState::kNotFound || state == SensorState::kInitFailed) {
    armRetry(static_cast<uint32_t>(millis()));
  } else {
    backoff_.reset();
  }
}

void PowerSensor::armRetry(uint32_t nowMs) { backoff_.armAfterFailure(nowMs); }

bool PowerSensor::begin() {
  ++attemptCount_;
  driverError_ = INA226_ERR_NONE;
  manufacturerId_ = 0;
  dieId_ = 0;

  const SensorError configError = validateAcquisitionConfig();
  if (configError != SensorError::kNone) {
    // A bad configuration will not fix itself; retrying is pointless but the
    // node stays alive so the operator can see the reason over serial.
    fail(SensorState::kConfigError, configError);
    return false;
  }

  // ESP8266 Wire takes (sda, scl).
  Wire.begin(static_cast<int>(cfg_.i2c.sdaPin), static_cast<int>(cfg_.i2c.sclPin));
  Wire.setClock(cfg_.i2c.clockHz);

  if (!device_.begin()) {
    fail(SensorState::kNotFound, SensorError::kNoI2cResponse);
    return false;
  }

  manufacturerId_ = device_.getManufacturerID();
  dieId_ = device_.getDieID();
  if (cfg_.sensor.requireIdentityCheck &&
      (manufacturerId_ != config::kIna226ManufacturerId || dieId_ != config::kIna226DieId)) {
    fail(SensorState::kInitFailed, SensorError::kIdentityMismatch);
    return false;
  }

  // Calibration is derived from the configured shunt and MAX_EXPECTED_CURRENT_A;
  // the driver's own defaults are never relied on (ADR-007).
  driverError_ = device_.setMaxCurrentShunt(cfg_.sensor.maxExpectedCurrentA,
                                            cfg_.sensor.shuntResistanceOhm, true);
  if (driverError_ != INA226_ERR_NONE) {
    fail(SensorState::kInitFailed, SensorError::kCalibrationRejected);
    return false;
  }
  if (!device_.isCalibrated()) {
    fail(SensorState::kInitFailed, SensorError::kNotCalibrated);
    return false;
  }
  if (!device_.setModeShuntBusContinuous()) {
    fail(SensorState::kInitFailed, SensorError::kModeSetFailed);
    return false;
  }

  currentLsbA_ = device_.getCurrentLSB();
  state_ = SensorState::kReady;
  error_ = SensorError::kNone;
  backoff_.reset();
  return true;
}

bool PowerSensor::tick(uint32_t nowMs) {
  if (state_ == SensorState::kReady || state_ == SensorState::kConfigError) {
    return false;
  }
  if (!backoff_.armed()) {
    // begin() has never run (kNotInitialized); start the first backoff window
    // here so the first attempt from loop() is still delayed, never immediate.
    armRetry(nowMs);
    return false;
  }
  // Rollover-safe: unsigned subtraction wraps correctly across millis() overflow.
  if (!backoff_.expired(nowMs)) {
    return false;
  }

  // fail() re-arms the window: 1s for the first failure, then 2s, 4s, ... capped 60s.
  begin();
  return true;
}

bool PowerSensor::isResponding() { return device_.isConnected(); }

measurement::PowerMeasurement PowerSensor::read() {
  using measurement::MeasurementStatus;
  using measurement::PowerMeasurement;

  PowerMeasurement sample = PowerMeasurement();
  sample.seq = ++seq_;  // every attempt consumes a sequence number
  sample.timestampMs = static_cast<uint32_t>(millis());
  sample.valid = false;

  if (!isReady()) {
    sample.status = MeasurementStatus::kSensorNotReady;
    return sample;
  }

  // Check transport before trusting any register content. Losing the device
  // mid-run sends the sensor back to the retry state instead of rebooting.
  if (!device_.isConnected()) {
    fail(SensorState::kNotFound, SensorError::kNoI2cResponse);
    sample.status = MeasurementStatus::kTransportFailed;
    return sample;
  }

  const float busVoltageV = device_.getBusVoltage();
  const float currentA = device_.getCurrent();
  // Canonical power is derived, not taken from the INA226 power register: that
  // register is an unsigned magnitude, so it would disagree in sign with a
  // negative (reverse-flow) current. PowerGuard needs signed power throughout.
  const float powerW = busVoltageV * currentA;

  // The INA226 raises this flag when the power register cannot represent the
  // result; the returned power would be meaningless.
  if ((device_.getAlertRegister() & INA226_MATH_OVERFLOW_FLAG) != 0) {
    sample.status = MeasurementStatus::kMathOverflow;
    return sample;
  }

  sample.status = measurement::validateElectricalValues(
      busVoltageV, currentA, powerW, cfg_.sensor.busVoltageMaxV,
      config::maxMeasurableCurrentA(cfg_.sensor));
  if (sample.status != MeasurementStatus::kOk) {
    return sample;
  }

  // Negative current is a legitimate reverse-flow reading and is preserved.
  sample.voltageV = busVoltageV;
  sample.currentA = currentA;
  sample.powerW = powerW;
  sample.valid = true;
  sample.status = MeasurementStatus::kOk;

  lastValid_ = sample;
  hasLastValid_ = true;
  return sample;
}

void PowerSensor::printStatus(Print& out) const {
  out.print(F("[sensor] INA226 "));
  out.print(describe(state_));
  out.print(F(" (attempt "));
  out.print(attemptCount_);
  out.println(F(")"));

  out.print(F("  address     : 0x"));
  out.println(cfg_.i2c.sensorAddress, HEX);
  out.print(F("  i2c pins    : SCL=GPIO"));
  out.print(cfg_.i2c.sclPin);
  out.print(F(" (D1), SDA=GPIO"));
  out.print(cfg_.i2c.sdaPin);
  out.println(F(" (D2)"));

  if (state_ == SensorState::kReady) {
    out.print(F("  detected    : YES, manufacturer=0x"));
    out.print(manufacturerId_, HEX);
    out.print(F(" die=0x"));
    out.println(dieId_, HEX);
    out.print(F("  calibration : shunt="));
    out.print(cfg_.sensor.shuntResistanceOhm, 4);
    out.print(F(" ohm, max_expected="));
    out.print(cfg_.sensor.maxExpectedCurrentA, 3);
    out.print(F(" A, current_lsb="));
    out.print(currentLsbA_ * 1e6f, 2);
    out.println(F(" uA/bit"));
    return;
  }

  out.print(F("  detected    : NO - "));
  out.println(describe(error_));
  if (error_ == SensorError::kIdentityMismatch) {
    out.print(F("  read ids    : manufacturer=0x"));
    out.print(manufacturerId_, HEX);
    out.print(F(" die=0x"));
    out.println(dieId_, HEX);
  }
  if (error_ == SensorError::kCalibrationRejected) {
    out.print(F("  driver code : 0x"));
    out.println(static_cast<uint16_t>(driverError_), HEX);
  }
  if (state_ == SensorState::kConfigError) {
    out.println(F("  retry       : disabled until configuration is corrected"));
  } else {
    out.print(F("  next retry  : "));
    out.print(backoff_.delayMs());
    out.println(F(" ms"));
  }
}

const char* describe(SensorState state) {
  switch (state) {
    case SensorState::kNotInitialized:
      return "NOT_INITIALIZED";
    case SensorState::kConfigError:
      return "CONFIG_ERROR";
    case SensorState::kNotFound:
      return "NOT_FOUND";
    case SensorState::kInitFailed:
      return "INIT_FAILED";
    case SensorState::kReady:
      return "READY";
  }
  return "UNKNOWN";
}

const char* describe(SensorError error) {
  switch (error) {
    case SensorError::kNone:
      return "no error";
    case SensorError::kShuntInvalid:
      return "SHUNT_RESISTANCE_OHM must be a positive finite value";
    case SensorError::kMaxCurrentMissing:
      return "MAX_EXPECTED_CURRENT_A: HARDWARE_CONFIGURATION_REQUIRED";
    case SensorError::kMaxCurrentUnmeasurable:
      return "MAX_EXPECTED_CURRENT_A exceeds INA226 shunt full-scale range";
    case SensorError::kAddressInvalid:
      return "INA226 address outside 0x40..0x4F";
    case SensorError::kPinsInvalid:
      return "I2C pin configuration invalid";
    case SensorError::kNoI2cResponse:
      return "no I2C response at the configured address";
    case SensorError::kIdentityMismatch:
      return "device is not an INA226 (manufacturer/die ID mismatch)";
    case SensorError::kCalibrationRejected:
      return "driver rejected calibration for shunt/max-current pair";
    case SensorError::kNotCalibrated:
      return "driver reported no current LSB after calibration";
    case SensorError::kModeSetFailed:
      return "failed to set shunt+bus continuous mode";
  }
  return "unknown sensor error";
}

}  // namespace sensor
}  // namespace powerguard
