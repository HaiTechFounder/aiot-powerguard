// Controllable INA226 and Wire fakes for host unit tests.

#include <INA226.h>
#include <Wire.h>

namespace {
Ina226FakeState g_ina;
WireFakeState g_wire;
}  // namespace

void ina226_fake_reset() {
  g_ina.connected = true;
  g_ina.manufacturerId = 0x5449;
  g_ina.dieId = 0x2260;
  g_ina.calibrationResult = INA226_ERR_NONE;
  g_ina.calibrationApplied = true;
  g_ina.modeSetSucceeds = true;
  g_ina.alertRegister = 0;
  g_ina.busVoltageV = 7.42f;
  g_ina.currentA = 0.312f;
  g_ina.connectedQueries = 0;
  g_ina.beginCalls = 0;

  g_wire.sdaPin = -1;
  g_wire.sclPin = -1;
  g_wire.clockHz = 0;
  g_wire.beginCalls = 0;
}

Ina226FakeState& ina226_fake() { return g_ina; }
WireFakeState& wire_fake() { return g_wire; }

TwoWire Wire;

void TwoWire::begin(int sda, int scl) {
  g_wire.sdaPin = sda;
  g_wire.sclPin = scl;
  ++g_wire.beginCalls;
}

void TwoWire::setClock(uint32_t clockHz) { g_wire.clockHz = clockHz; }

INA226::INA226(const uint8_t address, void*) : address_(address), currentLsb_(0.0f) {}

bool INA226::begin() {
  ++g_ina.beginCalls;
  return isConnected();
}

bool INA226::isConnected() {
  ++g_ina.connectedQueries;
  return g_ina.connected;
}

float INA226::getBusVoltage() { return g_ina.busVoltageV; }
float INA226::getCurrent() { return g_ina.currentA; }
float INA226::getPower() { return g_ina.busVoltageV * g_ina.currentA; }

int INA226::setMaxCurrentShunt(float maxCurrent, float shunt, bool) {
  if (g_ina.calibrationResult != INA226_ERR_NONE) {
    currentLsb_ = 0.0f;
    return g_ina.calibrationResult;
  }
  // Mirrors the driver's LSB derivation closely enough to assert that
  // calibration came from the configured values, not a driver default.
  currentLsb_ = g_ina.calibrationApplied ? maxCurrent / 32768.0f : 0.0f;
  (void)shunt;
  return INA226_ERR_NONE;
}

bool INA226::isCalibrated() { return currentLsb_ != 0.0f; }
float INA226::getCurrentLSB() { return currentLsb_; }
bool INA226::setModeShuntBusContinuous() { return g_ina.modeSetSucceeds; }
uint16_t INA226::getManufacturerID() { return g_ina.manufacturerId; }
uint16_t INA226::getDieID() { return g_ina.dieId; }
uint16_t INA226::getAlertRegister() { return g_ina.alertRegister; }
