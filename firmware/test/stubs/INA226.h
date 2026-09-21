// Controllable INA226 fake for host (native) unit tests.
//
// Lets the tests drive power_sensor.cpp - the real production file - through
// detection, identification, calibration and acquisition paths that would
// otherwise need hardware. It does NOT model I2C timing, register semantics or
// the driver's own arithmetic; those remain hardware-only concerns.

#pragma once

#include <stdint.h>

#define INA226_ERR_NONE 0x0000
#define INA226_ERR_SHUNTVOLTAGE_HIGH 0x8000
#define INA226_MATH_OVERFLOW_FLAG 0x0004

class INA226 {
 public:
  explicit INA226(const uint8_t address, void* wire = nullptr);

  bool begin();
  bool isConnected();
  float getBusVoltage();
  float getCurrent();
  float getPower();
  int setMaxCurrentShunt(float maxCurrent, float shunt, bool normalize);
  bool isCalibrated();
  float getCurrentLSB();
  bool setModeShuntBusContinuous();
  uint16_t getManufacturerID();
  uint16_t getDieID();
  uint16_t getAlertRegister();
  uint8_t getAddress() const { return address_; }

 private:
  uint8_t address_;
  float currentLsb_;
};

// ---------------------------------------------------------------------------
// Test control surface
// ---------------------------------------------------------------------------

struct Ina226FakeState {
  bool connected;
  uint16_t manufacturerId;
  uint16_t dieId;
  int calibrationResult;
  bool calibrationApplied;  // drives isCalibrated()
  bool modeSetSucceeds;
  uint16_t alertRegister;
  float busVoltageV;
  float currentA;
  uint32_t connectedQueries;
  uint32_t beginCalls;
};

// Resets to a healthy INA226 at 7.42 V / 0.312 A.
void ina226_fake_reset();
Ina226FakeState& ina226_fake();

// Wire stub bookkeeping, so tests can assert the configured pins were used.
struct WireFakeState {
  int sdaPin;
  int sclPin;
  uint32_t clockHz;
  uint32_t beginCalls;
};
WireFakeState& wire_fake();
