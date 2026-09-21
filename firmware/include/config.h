// AIoT PowerGuard - firmware configuration abstraction (P02-T02).
//
// Single source of configuration for the NodeMCU ESP8266 30-pin node.
// Non-secret defaults live here and may be overridden by PlatformIO `build_flags`.
// Secrets (Wi-Fi / MQTT credentials) come only from the Git-ignored `secrets.h`
// (copy `secrets_example.h`); they are never hard-coded in this file.
//
// This header declares configuration and validation only. No sensor, Wi-Fi or
// MQTT behavior is implemented here.

#pragma once

#include <stddef.h>
#include <stdint.h>

class Print;

namespace powerguard {
namespace config {

// ---------------------------------------------------------------------------
// Non-secret defaults (override via build_flags)
// ---------------------------------------------------------------------------

#ifndef POWERGUARD_FIRMWARE_VERSION
#define POWERGUARD_FIRMWARE_VERSION "0.1.0"
#endif

// Identity: must match ^[a-z0-9][a-z0-9_-]{0,31}$
#ifndef POWERGUARD_DEVICE_ID
#define POWERGUARD_DEVICE_ID "powerguard-01"
#endif

// I2C wiring is fixed by the hardware contract: NodeMCU D1 = GPIO5 (SCL),
// D2 = GPIO4 (SDA). Kept as macros so a different board revision stays buildable.
#ifndef POWERGUARD_I2C_SCL_PIN
#define POWERGUARD_I2C_SCL_PIN 5  // NodeMCU D1
#endif

#ifndef POWERGUARD_I2C_SDA_PIN
#define POWERGUARD_I2C_SDA_PIN 4  // NodeMCU D2
#endif

#ifndef POWERGUARD_INA226_ADDRESS
#define POWERGUARD_INA226_ADDRESS 0x40
#endif

// Authoritative installed shunt (ADR-007). Configurable, but 0.01 is the
// measured hardware value; calibration never falls back to a driver default.
#ifndef POWERGUARD_SHUNT_RESISTANCE_OHM
#define POWERGUARD_SHUNT_RESISTANCE_OHM 0.01f
#endif

// HARDWARE_CONFIGURATION_REQUIRED: selects the INA226 calibration range.
// It is NOT an alarm threshold. 0.0f means "not supplied" and fails validation
// on purpose - firmware must refuse to guess a safe current.
#ifndef POWERGUARD_MAX_EXPECTED_CURRENT_A
#define POWERGUARD_MAX_EXPECTED_CURRENT_A 0.0f
#endif

// 2S 18650 pack: 8.4 V fully charged. Upper bound of the plausible bus envelope.
#ifndef POWERGUARD_BUS_VOLTAGE_MAX_V
#define POWERGUARD_BUS_VOLTAGE_MAX_V 8.4f
#endif

// Verify the INA226 manufacturer/die ID during sensor bring-up.
#ifndef POWERGUARD_REQUIRE_SENSOR_IDENTITY
#define POWERGUARD_REQUIRE_SENSOR_IDENTITY 1
#endif

// I2C bus clock. INA226 supports up to 2.94 MHz; 100 kHz is the safe default
// for breadboard wiring.
#ifndef POWERGUARD_I2C_CLOCK_HZ
#define POWERGUARD_I2C_CLOCK_HZ 100000UL
#endif

#ifndef POWERGUARD_SENSOR_SAMPLE_INTERVAL_MS
#define POWERGUARD_SENSOR_SAMPLE_INTERVAL_MS 1000UL
#endif

#ifndef POWERGUARD_TELEMETRY_INTERVAL_MS
#define POWERGUARD_TELEMETRY_INTERVAL_MS 2000UL
#endif

#ifndef POWERGUARD_MQTT_PORT
#define POWERGUARD_MQTT_PORT 1883
#endif

// Wi-Fi association attempt budget before the attempt is abandoned and retried.
#ifndef POWERGUARD_WIFI_CONNECT_TIMEOUT_MS
#define POWERGUARD_WIFI_CONNECT_TIMEOUT_MS 15000UL
#endif

#ifndef POWERGUARD_WIFI_RECONNECT_INTERVAL_MS
#define POWERGUARD_WIFI_RECONNECT_INTERVAL_MS 1000UL
#endif

#ifndef POWERGUARD_WIFI_RECONNECT_MAX_INTERVAL_MS
#define POWERGUARD_WIFI_RECONNECT_MAX_INTERVAL_MS 60000UL
#endif

// Bounds how long a single MQTT connect may block the main loop.
#ifndef POWERGUARD_MQTT_CONNECT_TIMEOUT_MS
#define POWERGUARD_MQTT_CONNECT_TIMEOUT_MS 4000UL
#endif

#ifndef POWERGUARD_MQTT_KEEPALIVE_MS
#define POWERGUARD_MQTT_KEEPALIVE_MS 30000UL
#endif

// Bounded RAM FIFO of telemetry snapshots held while the broker is unreachable
// (MQTT_SPEC: oldest dropped on overflow, no flash spool).
#ifndef POWERGUARD_TELEMETRY_QUEUE_CAPACITY
#define POWERGUARD_TELEMETRY_QUEUE_CAPACITY 16
#endif

// Jitter added on top of every retry window, in percent of the base delay.
#ifndef POWERGUARD_BACKOFF_JITTER_PERCENT
#define POWERGUARD_BACKOFF_JITTER_PERCENT 20
#endif

#ifndef POWERGUARD_MQTT_RECONNECT_INTERVAL_MS
#define POWERGUARD_MQTT_RECONNECT_INTERVAL_MS 5000UL
#endif

// Spec cap for every retry domain.
#ifndef POWERGUARD_MQTT_RECONNECT_MAX_INTERVAL_MS
#define POWERGUARD_MQTT_RECONNECT_MAX_INTERVAL_MS 60000UL
#endif

// Trusted-LAN mode: when 1, an empty MQTT username is accepted.
#ifndef POWERGUARD_MQTT_ALLOW_ANONYMOUS
#define POWERGUARD_MQTT_ALLOW_ANONYMOUS 0
#endif

// Operational thresholds stay disabled until experimentally approved (ADR-007).
// No guessed defaults: enabling one without a value is a validation error.
#ifndef POWERGUARD_WARNING_CURRENT_ENABLED
#define POWERGUARD_WARNING_CURRENT_ENABLED 0
#endif

#ifndef POWERGUARD_WARNING_CURRENT_A
#define POWERGUARD_WARNING_CURRENT_A 0.0f
#endif

#ifndef POWERGUARD_OVERCURRENT_ENABLED
#define POWERGUARD_OVERCURRENT_ENABLED 0
#endif

#ifndef POWERGUARD_OVERCURRENT_THRESHOLD_A
#define POWERGUARD_OVERCURRENT_THRESHOLD_A 0.0f
#endif

// INA226 differential shunt-voltage full scale (datasheet): +/- 81.92 mV.
// robtillaart/INA226 0.6.6 rejects calibration above 81.90 mV to leave headroom
// against math overflow, so the usable ceiling here matches the driver limit.
// Used to derive the measurable current ceiling from the configured shunt.
constexpr float kIna226ShuntVoltageFullScaleV = 0.08190f;

// INA226 identity registers (datasheet).
constexpr uint16_t kIna226ManufacturerId = 0x5449;
constexpr uint16_t kIna226DieId = 0x2260;

constexpr size_t kDeviceIdMaxLength = 32;
constexpr size_t kFirmwareVersionMaxLength = 32;

// ---------------------------------------------------------------------------
// Typed configuration
// ---------------------------------------------------------------------------

struct IdentityConfig {
  const char* deviceId;
  const char* firmwareVersion;
};

struct I2cConfig {
  uint8_t sclPin;
  uint8_t sdaPin;
  uint8_t sensorAddress;
  uint32_t clockHz;
};

struct SensorConfig {
  float shuntResistanceOhm;
  float maxExpectedCurrentA;  // 0.0f => HARDWARE_CONFIGURATION_REQUIRED
  float busVoltageMaxV;
  // Reject a device whose manufacturer/die ID is not INA226. Some clone boards
  // report different IDs; disable only after confirming the part is genuine.
  bool requireIdentityCheck;
};

struct TimingConfig {
  uint32_t sampleIntervalMs;
  uint32_t telemetryIntervalMs;
};

struct WifiConfig {
  const char* ssid;
  const char* password;
  uint32_t connectTimeoutMs;
  uint32_t reconnectIntervalMs;
  uint32_t reconnectMaxIntervalMs;
};

struct MqttConfig {
  const char* host;
  uint16_t port;
  const char* username;
  const char* password;
  uint32_t reconnectIntervalMs;
  uint32_t reconnectMaxIntervalMs;
  uint32_t connectTimeoutMs;
  uint32_t keepAliveMs;
  bool allowAnonymous;
};

// An operational threshold is meaningful only when explicitly enabled.
struct CurrentThreshold {
  bool enabled;
  float valueA;
};

struct ThresholdConfig {
  CurrentThreshold warning;
  CurrentThreshold overcurrent;
};

struct Config {
  IdentityConfig identity;
  I2cConfig i2c;
  SensorConfig sensor;
  TimingConfig timing;
  WifiConfig wifi;
  MqttConfig mqtt;
  ThresholdConfig thresholds;
  bool secretsFilePresent;  // false when secrets.h was not found at compile time
};

// The single configuration instance for this firmware image.
const Config& config();

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

enum class ConfigError : uint8_t {
  kNone = 0,
  kDeviceIdInvalid,
  kFirmwareVersionMissing,
  kFirmwareVersionInvalid,
  kI2cPinInvalid,
  kI2cPinsIdentical,
  kSensorAddressInvalid,
  kShuntResistanceInvalid,
  kMaxExpectedCurrentMissing,
  kMaxExpectedCurrentUnmeasurable,
  kBusVoltageMaxInvalid,
  kSampleIntervalInvalid,
  kTelemetryIntervalInvalid,
  kTelemetryFasterThanSample,
  kSecretsFileMissing,
  kWifiSsidMissing,
  kMqttHostMissing,
  kMqttPortInvalid,
  kMqttCredentialsMissing,
  kMqttReconnectIntervalInvalid,
  kMqttReconnectCapInvalid,
  kMqttTimeoutInvalid,
  kWifiTimingInvalid,
  kWarningThresholdInvalid,
  kOvercurrentThresholdInvalid,
  kThresholdOrderInvalid,
};

constexpr size_t kMaxConfigErrors = 10;

struct ConfigValidation {
  ConfigError errors[kMaxConfigErrors];
  size_t errorCount;
  bool truncated;  // more errors existed than the fixed report can hold

  bool ok() const { return errorCount == 0; }
};

// Pure function: no I/O, no globals. Safe to unit test on the host (P02-T11).
ConfigValidation validate(const Config& cfg);

// Static, human-readable reason. Never contains configured values or secrets.
const char* describe(ConfigError error);

// Highest current the hardware can measure: INA226 shunt full scale / shunt R.
// This is a measurement ceiling, never a safe operating current.
float maxMeasurableCurrentA(const SensorConfig& sensor);

// ^[a-z0-9][a-z0-9_-]{0,31}$
bool isValidDeviceId(const char* deviceId);

// SemVer MAJOR.MINOR.PATCH with optional -prerelease / +build suffix,
// at most kFirmwareVersionMaxLength characters (MQTT_SPEC).
bool isValidSemVer(const char* version);

// ---------------------------------------------------------------------------
// Diagnostics (secrets redacted)
// ---------------------------------------------------------------------------

void printSummary(Print& out, const Config& cfg);
void printValidation(Print& out, const ConfigValidation& report);

}  // namespace config
}  // namespace powerguard
