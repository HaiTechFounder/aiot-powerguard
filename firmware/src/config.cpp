// AIoT PowerGuard - configuration instance, validation and redacted diagnostics.

// Secrets are optional at compile time so the project still builds on a machine
// (or CI) without local credentials. A missing file is reported by validation
// instead of silently using a baked-in placeholder.
//
// This block must come before "config.h": config.h supplies `#ifndef` defaults,
// so anything secrets.h defines has to be visible first to take precedence.
#if defined(__has_include)
#if __has_include("secrets.h")
#include "secrets.h"
#define POWERGUARD_SECRETS_FILE_PRESENT 1
#endif
#endif

#include "config.h"

#include <Arduino.h>
#include <math.h>
#include <string.h>

#ifndef POWERGUARD_SECRETS_FILE_PRESENT
#define POWERGUARD_SECRETS_FILE_PRESENT 0
#endif

#ifndef POWERGUARD_WIFI_SSID
#define POWERGUARD_WIFI_SSID ""
#endif
#ifndef POWERGUARD_WIFI_PASSWORD
#define POWERGUARD_WIFI_PASSWORD ""
#endif
#ifndef POWERGUARD_MQTT_HOST
#define POWERGUARD_MQTT_HOST ""
#endif
#ifndef POWERGUARD_MQTT_USERNAME
#define POWERGUARD_MQTT_USERNAME ""
#endif
#ifndef POWERGUARD_MQTT_PASSWORD
#define POWERGUARD_MQTT_PASSWORD ""
#endif

namespace powerguard {
namespace config {
namespace {

const Config kConfig = {
    // identity
    {POWERGUARD_DEVICE_ID, POWERGUARD_FIRMWARE_VERSION},
    // i2c
    {static_cast<uint8_t>(POWERGUARD_I2C_SCL_PIN),
     static_cast<uint8_t>(POWERGUARD_I2C_SDA_PIN),
     static_cast<uint8_t>(POWERGUARD_INA226_ADDRESS),
     static_cast<uint32_t>(POWERGUARD_I2C_CLOCK_HZ)},
    // sensor
    {POWERGUARD_SHUNT_RESISTANCE_OHM,
     POWERGUARD_MAX_EXPECTED_CURRENT_A,
     POWERGUARD_BUS_VOLTAGE_MAX_V,
     POWERGUARD_REQUIRE_SENSOR_IDENTITY != 0},
    // timing
    {static_cast<uint32_t>(POWERGUARD_SENSOR_SAMPLE_INTERVAL_MS),
     static_cast<uint32_t>(POWERGUARD_TELEMETRY_INTERVAL_MS)},
    // wifi
    {POWERGUARD_WIFI_SSID,
     POWERGUARD_WIFI_PASSWORD,
     static_cast<uint32_t>(POWERGUARD_WIFI_CONNECT_TIMEOUT_MS),
     static_cast<uint32_t>(POWERGUARD_WIFI_RECONNECT_INTERVAL_MS),
     static_cast<uint32_t>(POWERGUARD_WIFI_RECONNECT_MAX_INTERVAL_MS)},
    // mqtt
    {POWERGUARD_MQTT_HOST,
     static_cast<uint16_t>(POWERGUARD_MQTT_PORT),
     POWERGUARD_MQTT_USERNAME,
     POWERGUARD_MQTT_PASSWORD,
     static_cast<uint32_t>(POWERGUARD_MQTT_RECONNECT_INTERVAL_MS),
     static_cast<uint32_t>(POWERGUARD_MQTT_RECONNECT_MAX_INTERVAL_MS),
     static_cast<uint32_t>(POWERGUARD_MQTT_CONNECT_TIMEOUT_MS),
     static_cast<uint32_t>(POWERGUARD_MQTT_KEEPALIVE_MS),
     POWERGUARD_MQTT_ALLOW_ANONYMOUS != 0},
    // thresholds
    {{POWERGUARD_WARNING_CURRENT_ENABLED != 0, POWERGUARD_WARNING_CURRENT_A},
     {POWERGUARD_OVERCURRENT_ENABLED != 0, POWERGUARD_OVERCURRENT_THRESHOLD_A}},
    // secretsFilePresent
    POWERGUARD_SECRETS_FILE_PRESENT != 0,
};

bool isBlank(const char* value) { return value == nullptr || value[0] == '\0'; }

bool isDigitChar(char c) { return c >= '0' && c <= '9'; }

// SemVer identifier alphabet: [0-9A-Za-z-]
bool isSemVerIdentChar(char c) {
  return isDigitChar(c) || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '-';
}

// Consumes a dot-separated list of non-empty identifiers starting at `i`.
// Stops cleanly at end of string or at `stopChar` (use '\0' for none).
// With `numericNoLeadingZero`, an all-digit identifier may not start with 0
// unless it is exactly "0".
bool parseSemVerIdentifiers(const char* version, size_t length, size_t& i,
                            bool numericNoLeadingZero, char stopChar) {
  for (;;) {
    const size_t start = i;
    bool allDigits = true;
    while (i < length && isSemVerIdentChar(version[i])) {
      if (!isDigitChar(version[i])) {
        allDigits = false;
      }
      ++i;
    }
    if (i == start) {
      return false;  // empty identifier
    }
    if (numericNoLeadingZero && allDigits && (i - start) > 1 && version[start] == '0') {
      return false;
    }
    if (i == length) {
      return true;
    }
    if (version[i] == '.') {
      ++i;
      continue;  // next identifier
    }
    if (stopChar != '\0' && version[i] == stopChar) {
      return true;
    }
    return false;  // character outside the identifier alphabet
  }
}

bool isPositiveFinite(float value) { return isfinite(value) && value > 0.0f; }

// ESP8266 exposes GPIO0..GPIO16; higher numbers are not addressable pins.
bool isUsablePin(uint8_t pin) { return pin <= 16; }

class ErrorCollector {
 public:
  explicit ErrorCollector(ConfigValidation& report) : report_(report) {
    report_.errorCount = 0;
    report_.truncated = false;
  }

  void add(ConfigError error) {
    if (report_.errorCount < kMaxConfigErrors) {
      report_.errors[report_.errorCount++] = error;
    } else {
      report_.truncated = true;
    }
  }

 private:
  ConfigValidation& report_;
};

void printRedacted(Print& out, const char* value) {
  out.print(isBlank(value) ? F("<missing>") : F("<set>"));
}

}  // namespace

const Config& config() { return kConfig; }

bool isValidDeviceId(const char* deviceId) {
  if (isBlank(deviceId)) {
    return false;
  }
  const size_t length = strnlen(deviceId, kDeviceIdMaxLength + 1);
  if (length > kDeviceIdMaxLength) {
    return false;
  }
  const char first = deviceId[0];
  const bool firstOk = (first >= 'a' && first <= 'z') || (first >= '0' && first <= '9');
  if (!firstOk) {
    return false;
  }
  for (size_t i = 1; i < length; ++i) {
    const char c = deviceId[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
    if (!ok) {
      return false;
    }
  }
  return true;
}

bool isValidSemVer(const char* version) {
  if (isBlank(version)) {
    return false;
  }
  const size_t length = strnlen(version, kFirmwareVersionMaxLength + 1);
  if (length > kFirmwareVersionMaxLength) {
    return false;
  }

  // Core: MAJOR.MINOR.PATCH, each a digit run with no redundant leading zero.
  size_t i = 0;
  for (uint8_t part = 0; part < 3; ++part) {
    const size_t start = i;
    while (i < length && isDigitChar(version[i])) {
      ++i;
    }
    const size_t digits = i - start;
    if (digits == 0 || (digits > 1 && version[start] == '0')) {
      return false;
    }
    if (part < 2) {
      if (i >= length || version[i] != '.') {
        return false;
      }
      ++i;  // consume the separator
    }
  }
  if (i == length) {
    return true;
  }

  // Optional prerelease: dot-separated non-empty [0-9A-Za-z-] identifiers.
  // A purely numeric prerelease identifier must not carry a leading zero.
  if (version[i] == '-') {
    ++i;
    if (!parseSemVerIdentifiers(version, length, i, true, '+')) {
      return false;
    }
    if (i == length) {
      return true;
    }
  }

  // Optional build metadata: same identifier grammar, leading zeros allowed.
  if (version[i] != '+') {
    return false;
  }
  ++i;
  if (!parseSemVerIdentifiers(version, length, i, false, '\0')) {
    return false;
  }
  return i == length;
}

float maxMeasurableCurrentA(const SensorConfig& sensor) {
  if (!isPositiveFinite(sensor.shuntResistanceOhm)) {
    return 0.0f;
  }
  return kIna226ShuntVoltageFullScaleV / sensor.shuntResistanceOhm;
}

ConfigValidation validate(const Config& cfg) {
  ConfigValidation report;
  ErrorCollector errors(report);

  // Identity
  if (!isValidDeviceId(cfg.identity.deviceId)) {
    errors.add(ConfigError::kDeviceIdInvalid);
  }
  if (isBlank(cfg.identity.firmwareVersion)) {
    errors.add(ConfigError::kFirmwareVersionMissing);
  } else if (!isValidSemVer(cfg.identity.firmwareVersion)) {
    errors.add(ConfigError::kFirmwareVersionInvalid);
  }

  // I2C
  if (!isUsablePin(cfg.i2c.sclPin) || !isUsablePin(cfg.i2c.sdaPin)) {
    errors.add(ConfigError::kI2cPinInvalid);
  } else if (cfg.i2c.sclPin == cfg.i2c.sdaPin) {
    errors.add(ConfigError::kI2cPinsIdentical);
  }
  if (cfg.i2c.sensorAddress < 0x40 || cfg.i2c.sensorAddress > 0x4F) {
    errors.add(ConfigError::kSensorAddressInvalid);
  }

  // Acquisition
  const bool shuntOk = isPositiveFinite(cfg.sensor.shuntResistanceOhm);
  if (!shuntOk) {
    errors.add(ConfigError::kShuntResistanceInvalid);
  }
  if (!isPositiveFinite(cfg.sensor.maxExpectedCurrentA)) {
    errors.add(ConfigError::kMaxExpectedCurrentMissing);
  } else if (shuntOk && cfg.sensor.maxExpectedCurrentA > maxMeasurableCurrentA(cfg.sensor)) {
    errors.add(ConfigError::kMaxExpectedCurrentUnmeasurable);
  }
  if (!isPositiveFinite(cfg.sensor.busVoltageMaxV)) {
    errors.add(ConfigError::kBusVoltageMaxInvalid);
  }

  // Timing
  if (cfg.timing.sampleIntervalMs == 0) {
    errors.add(ConfigError::kSampleIntervalInvalid);
  }
  if (cfg.timing.telemetryIntervalMs == 0) {
    errors.add(ConfigError::kTelemetryIntervalInvalid);
  } else if (cfg.timing.sampleIntervalMs != 0 &&
             cfg.timing.telemetryIntervalMs < cfg.timing.sampleIntervalMs) {
    errors.add(ConfigError::kTelemetryFasterThanSample);
  }

  // Secrets
  if (!cfg.secretsFilePresent) {
    errors.add(ConfigError::kSecretsFileMissing);
  }
  if (isBlank(cfg.wifi.ssid)) {
    errors.add(ConfigError::kWifiSsidMissing);
  }
  if (isBlank(cfg.mqtt.host)) {
    errors.add(ConfigError::kMqttHostMissing);
  }
  if (cfg.mqtt.port == 0) {
    errors.add(ConfigError::kMqttPortInvalid);
  }
  // A broker account is only usable with both parts; MQTT_SPEC disables
  // anonymous connections by default.
  if (!cfg.mqtt.allowAnonymous && (isBlank(cfg.mqtt.username) || isBlank(cfg.mqtt.password))) {
    errors.add(ConfigError::kMqttCredentialsMissing);
  }
  if (cfg.mqtt.reconnectIntervalMs == 0) {
    errors.add(ConfigError::kMqttReconnectIntervalInvalid);
  }
  if (cfg.mqtt.reconnectMaxIntervalMs < cfg.mqtt.reconnectIntervalMs ||
      cfg.mqtt.reconnectMaxIntervalMs > 60000UL) {
    errors.add(ConfigError::kMqttReconnectCapInvalid);
  }
  if (cfg.mqtt.connectTimeoutMs == 0 || cfg.mqtt.keepAliveMs == 0) {
    errors.add(ConfigError::kMqttTimeoutInvalid);
  }
  if (cfg.wifi.connectTimeoutMs == 0 || cfg.wifi.reconnectIntervalMs == 0 ||
      cfg.wifi.reconnectMaxIntervalMs < cfg.wifi.reconnectIntervalMs ||
      cfg.wifi.reconnectMaxIntervalMs > 60000UL) {
    errors.add(ConfigError::kWifiTimingInvalid);
  }

  // Operational thresholds: only checked when explicitly enabled.
  const CurrentThreshold& warning = cfg.thresholds.warning;
  const CurrentThreshold& overcurrent = cfg.thresholds.overcurrent;
  if (warning.enabled && !isPositiveFinite(warning.valueA)) {
    errors.add(ConfigError::kWarningThresholdInvalid);
  }
  if (overcurrent.enabled && !isPositiveFinite(overcurrent.valueA)) {
    errors.add(ConfigError::kOvercurrentThresholdInvalid);
  }
  if (warning.enabled && overcurrent.enabled && isfinite(warning.valueA) &&
      isfinite(overcurrent.valueA) && warning.valueA > overcurrent.valueA) {
    errors.add(ConfigError::kThresholdOrderInvalid);
  }

  return report;
}

const char* describe(ConfigError error) {
  switch (error) {
    case ConfigError::kNone:
      return "no error";
    case ConfigError::kDeviceIdInvalid:
      return "DEVICE_ID must match ^[a-z0-9][a-z0-9_-]{0,31}$";
    case ConfigError::kFirmwareVersionMissing:
      return "FIRMWARE_VERSION is empty";
    case ConfigError::kFirmwareVersionInvalid:
      return "FIRMWARE_VERSION must be SemVer MAJOR.MINOR.PATCH, max 32 characters";
    case ConfigError::kI2cPinInvalid:
      return "I2C pin outside GPIO0..GPIO16";
    case ConfigError::kI2cPinsIdentical:
      return "I2C SCL and SDA pins are identical";
    case ConfigError::kSensorAddressInvalid:
      return "INA226 address outside 0x40..0x4F";
    case ConfigError::kShuntResistanceInvalid:
      return "SHUNT_RESISTANCE_OHM must be a positive finite value";
    case ConfigError::kMaxExpectedCurrentMissing:
      return "MAX_EXPECTED_CURRENT_A: HARDWARE_CONFIGURATION_REQUIRED";
    case ConfigError::kMaxExpectedCurrentUnmeasurable:
      return "MAX_EXPECTED_CURRENT_A exceeds INA226 shunt full-scale range";
    case ConfigError::kBusVoltageMaxInvalid:
      return "BUS_VOLTAGE_MAX_V must be a positive finite value";
    case ConfigError::kSampleIntervalInvalid:
      return "SENSOR_SAMPLE_INTERVAL_MS must be > 0";
    case ConfigError::kTelemetryIntervalInvalid:
      return "TELEMETRY_INTERVAL_MS must be > 0";
    case ConfigError::kTelemetryFasterThanSample:
      return "TELEMETRY_INTERVAL_MS must not be shorter than the sample interval";
    case ConfigError::kSecretsFileMissing:
      return "secrets.h not found: copy include/secrets_example.h to include/secrets.h";
    case ConfigError::kWifiSsidMissing:
      return "WIFI_SSID is empty";
    case ConfigError::kMqttHostMissing:
      return "MQTT_HOST is empty";
    case ConfigError::kMqttPortInvalid:
      return "MQTT_PORT must be > 0";
    case ConfigError::kMqttCredentialsMissing:
      return "MQTT_USERNAME is empty and anonymous mode is disabled";
    case ConfigError::kMqttReconnectIntervalInvalid:
      return "MQTT_RECONNECT_INTERVAL_MS must be > 0";
    case ConfigError::kMqttReconnectCapInvalid:
      return "MQTT reconnect cap must be >= base interval and <= 60000 ms";
    case ConfigError::kMqttTimeoutInvalid:
      return "MQTT connect timeout and keep-alive must be > 0";
    case ConfigError::kWifiTimingInvalid:
      return "Wi-Fi connect timeout / reconnect window invalid (cap <= 60000 ms)";
    case ConfigError::kWarningThresholdInvalid:
      return "WARNING_CURRENT_A enabled without a positive value";
    case ConfigError::kOvercurrentThresholdInvalid:
      return "OVERCURRENT_THRESHOLD_A enabled without a positive value";
    case ConfigError::kThresholdOrderInvalid:
      return "WARNING_CURRENT_A must not exceed OVERCURRENT_THRESHOLD_A";
  }
  return "unknown configuration error";
}

void printSummary(Print& out, const Config& cfg) {
  out.println(F("[config] summary"));
  out.print(F("  device_id        : "));
  out.println(cfg.identity.deviceId);
  out.print(F("  firmware_version : "));
  out.println(cfg.identity.firmwareVersion);
  out.print(F("  i2c              : SCL=GPIO"));
  out.print(cfg.i2c.sclPin);
  out.print(F(" (D1), SDA=GPIO"));
  out.print(cfg.i2c.sdaPin);
  out.print(F(" (D2), addr=0x"));
  out.println(cfg.i2c.sensorAddress, HEX);
  out.print(F("  shunt_ohm        : "));
  out.println(cfg.sensor.shuntResistanceOhm, 4);
  out.print(F("  max_expected_a   : "));
  if (cfg.sensor.maxExpectedCurrentA > 0.0f) {
    out.println(cfg.sensor.maxExpectedCurrentA, 3);
  } else {
    out.println(F("HARDWARE_CONFIGURATION_REQUIRED"));
  }
  out.print(F("  measurable_max_a : "));
  out.println(maxMeasurableCurrentA(cfg.sensor), 3);
  out.print(F("  bus_voltage_max_v: "));
  out.println(cfg.sensor.busVoltageMaxV, 2);
  out.print(F("  sample_interval  : "));
  out.print(cfg.timing.sampleIntervalMs);
  out.println(F(" ms"));
  out.print(F("  telemetry_interval: "));
  out.print(cfg.timing.telemetryIntervalMs);
  out.println(F(" ms"));
  out.print(F("  wifi_ssid        : "));
  printRedacted(out, cfg.wifi.ssid);
  out.println();
  out.print(F("  wifi_password    : "));
  printRedacted(out, cfg.wifi.password);
  out.println();
  out.print(F("  mqtt_host        : "));
  printRedacted(out, cfg.mqtt.host);
  out.print(F(" port="));
  out.println(cfg.mqtt.port);
  out.print(F("  mqtt_username    : "));
  printRedacted(out, cfg.mqtt.username);
  out.print(F(" anonymous="));
  out.println(cfg.mqtt.allowAnonymous ? F("yes") : F("no"));
  out.print(F("  mqtt_password    : "));
  printRedacted(out, cfg.mqtt.password);
  out.println();
  out.print(F("  mqtt_reconnect   : "));
  out.print(cfg.mqtt.reconnectIntervalMs);
  out.print(F(" ms, cap "));
  out.print(cfg.mqtt.reconnectMaxIntervalMs);
  out.println(F(" ms"));
  out.print(F("  warning_current  : "));
  if (cfg.thresholds.warning.enabled) {
    out.print(cfg.thresholds.warning.valueA, 3);
    out.println(F(" A"));
  } else {
    out.println(F("disabled"));
  }
  out.print(F("  overcurrent      : "));
  if (cfg.thresholds.overcurrent.enabled) {
    out.print(cfg.thresholds.overcurrent.valueA, 3);
    out.println(F(" A"));
  } else {
    out.println(F("disabled"));
  }
}

void printValidation(Print& out, const ConfigValidation& report) {
  if (report.ok()) {
    out.println(F("[config] OK"));
    return;
  }
  out.print(F("[config] INVALID: "));
  out.print(report.errorCount);
  out.println(F(" problem(s)"));
  for (size_t i = 0; i < report.errorCount; ++i) {
    out.print(F("  - "));
    out.println(describe(report.errors[i]));
  }
  if (report.truncated) {
    out.println(F("  - (additional problems not shown)"));
  }
}

}  // namespace config
}  // namespace powerguard
