// AIoT PowerGuard - host unit tests (P02-T11).
//
// Runs on the development machine via `pio test -e native`.
//
// power_sensor.cpp IS compiled here and driven through a controllable INA226 /
// Wire fake, so detection, calibration and acquisition follow the production
// code path. wifi_manager.cpp and mqtt_manager.cpp are excluded: they depend on
// the ESP8266 Wi-Fi stack and a live broker, so their behaviour stays hardware
// and network evidence (docs/hardware-test-checklist.md).

#include <Arduino.h>
#include <stdlib.h>
#include <string.h>
#include <unity.h>

#include <string>
#include <vector>

#include "backoff.h"
#include "config.h"
#include "diagnostics.h"
#include "measurement.h"
#include "power_sensor.h"
#include "scheduler.h"
#include "telemetry.h"

#include <INA226.h>
#include <Wire.h>

using powerguard::config::Config;
using powerguard::config::ConfigError;
using powerguard::config::ConfigValidation;
using powerguard::measurement::EnergyIntegrator;
using powerguard::measurement::MeasurementStatus;
using powerguard::measurement::PowerMeasurement;
using powerguard::net::Backoff;
using powerguard::sched::Deadline;
using powerguard::sensor::PowerSensor;
using powerguard::sensor::SensorError;
using powerguard::sensor::SensorState;
using powerguard::telemetry::TelemetryQueue;
using powerguard::telemetry::TelemetrySample;

namespace {

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

class StringPrint : public Print {
 public:
  size_t write(const char* text) override {
    buffer += text;
    return strlen(text);
  }
  void clear() { buffer.clear(); }
  bool contains(const char* needle) const { return buffer.find(needle) != std::string::npos; }
  size_t count(const char* needle) const {
    size_t n = 0;
    size_t pos = buffer.find(needle);
    while (pos != std::string::npos) {
      ++n;
      pos = buffer.find(needle, pos + 1);
    }
    return n;
  }
  std::string buffer;
};

Config makeValidConfig() {
  Config cfg = {};
  cfg.identity.deviceId = "powerguard-01";
  cfg.identity.firmwareVersion = "0.1.0";
  cfg.i2c.sclPin = 5;
  cfg.i2c.sdaPin = 4;
  cfg.i2c.sensorAddress = 0x40;
  cfg.i2c.clockHz = 100000UL;
  cfg.sensor.shuntResistanceOhm = 0.01f;
  cfg.sensor.maxExpectedCurrentA = 5.0f;
  cfg.sensor.busVoltageMaxV = 8.4f;
  cfg.sensor.requireIdentityCheck = true;
  cfg.timing.sampleIntervalMs = 1000UL;
  cfg.timing.telemetryIntervalMs = 2000UL;
  cfg.wifi.ssid = "lan";
  cfg.wifi.password = "secret";
  cfg.wifi.connectTimeoutMs = 15000UL;
  cfg.wifi.reconnectIntervalMs = 1000UL;
  cfg.wifi.reconnectMaxIntervalMs = 60000UL;
  cfg.mqtt.host = "192.168.1.10";
  cfg.mqtt.port = 1883;
  cfg.mqtt.username = "device";
  cfg.mqtt.password = "pw";
  cfg.mqtt.reconnectIntervalMs = 5000UL;
  cfg.mqtt.reconnectMaxIntervalMs = 60000UL;
  cfg.mqtt.connectTimeoutMs = 4000UL;
  cfg.mqtt.keepAliveMs = 30000UL;
  cfg.mqtt.allowAnonymous = false;
  cfg.thresholds.warning.enabled = false;
  cfg.thresholds.warning.valueA = 0.0f;
  cfg.thresholds.overcurrent.enabled = false;
  cfg.thresholds.overcurrent.valueA = 0.0f;
  cfg.secretsFilePresent = true;
  return cfg;
}

bool hasError(const ConfigValidation& report, ConfigError wanted) {
  for (size_t i = 0; i < report.errorCount; ++i) {
    if (report.errors[i] == wanted) {
      return true;
    }
  }
  return false;
}

PowerMeasurement makeSample(uint32_t seq, uint32_t ts, float v, float i, bool valid = true) {
  PowerMeasurement m = PowerMeasurement();
  m.seq = seq;
  m.timestampMs = ts;
  m.voltageV = valid ? v : 0.0f;
  m.currentA = valid ? i : 0.0f;
  m.powerW = valid ? v * i : 0.0f;
  m.valid = valid;
  m.status = valid ? MeasurementStatus::kOk : MeasurementStatus::kTransportFailed;
  return m;
}

// ---------------------------------------------------------------------------
// A tiny reader for the flat JSON the firmware emits. It is deliberately strict
// about structure so a malformed payload fails the test rather than silently
// matching: no nesting, no escapes, exactly `{"k":v,...}`.
// ---------------------------------------------------------------------------

struct JsonField {
  std::string key;
  std::string raw;  // value exactly as it appears, quotes included
};

bool isJsonString(const std::string& raw) {
  return raw.size() >= 2 && raw.front() == '"' && raw.back() == '"';
}

std::string jsonStringValue(const std::string& raw) { return raw.substr(1, raw.size() - 2); }

bool isJsonNull(const std::string& raw) { return raw == "null"; }

bool isJsonNumber(const std::string& raw) {
  if (raw.empty()) {
    return false;
  }
  size_t i = (raw[0] == '-') ? 1 : 0;
  if (i >= raw.size()) {
    return false;
  }
  bool digit = false;
  bool dot = false;
  for (; i < raw.size(); ++i) {
    if (raw[i] >= '0' && raw[i] <= '9') {
      digit = true;
    } else if (raw[i] == '.' && !dot && digit) {
      dot = true;
      digit = false;  // at least one digit must follow the point
    } else {
      return false;  // no exponent, sign run, NaN or Infinity in a v1 payload
    }
  }
  return digit;
}

bool parseFlatJson(const char* text, std::vector<JsonField>& out) {
  out.clear();
  const std::string json(text);
  if (json.size() < 2 || json.front() != '{' || json.back() != '}') {
    return false;
  }
  size_t i = 1;
  const size_t end = json.size() - 1;
  if (i == end) {
    return false;  // an empty object is never a valid v1 payload
  }
  for (;;) {
    if (i >= end || json[i] != '"') {
      return false;
    }
    const size_t keyStart = ++i;
    while (i < end && json[i] != '"') {
      if (json[i] == 0x5C) {
        return false;  // escapes are not expected in v1 keys
      }
      ++i;
    }
    if (i >= end || i == keyStart) {
      return false;  // unterminated or empty key
    }
    JsonField field;
    field.key = json.substr(keyStart, i - keyStart);
    ++i;  // closing quote
    if (i >= end || json[i] != ':') {
      return false;
    }
    ++i;
    if (i >= end) {
      return false;  // missing value
    }

    const size_t valueStart = i;
    if (json[i] == '"') {
      ++i;
      while (i < end && json[i] != '"') {
        if (json[i] == 0x5C) {
          return false;  // escapes are not expected in v1 payloads
        }
        ++i;
      }
      if (i >= end) {
        return false;  // unterminated string
      }
      ++i;  // closing quote
      field.raw = json.substr(valueStart, i - valueStart);
    } else {
      while (i < end && json[i] != ',') {
        ++i;
      }
      field.raw = json.substr(valueStart, i - valueStart);
      // A bare token must be a JSON number or null; nothing else is legal here.
      if (!isJsonNumber(field.raw) && !isJsonNull(field.raw)) {
        return false;
      }
    }

    for (const JsonField& seen : out) {
      if (seen.key == field.key) {
        return false;  // duplicate key
      }
    }
    out.push_back(field);

    if (i == end) {
      return true;  // object closes right after this value
    }
    if (json[i] != ',') {
      return false;  // junk between members
    }
    ++i;
    if (i >= end) {
      return false;  // trailing comma
    }
  }
}

const JsonField* findField(const std::vector<JsonField>& fields, const char* key) {
  for (const JsonField& f : fields) {
    if (f.key == key) {
      return &f;
    }
  }
  return nullptr;
}

}  // namespace

// ---------------------------------------------------------------------------
// config validation
// ---------------------------------------------------------------------------

void test_config_valid_baseline_has_no_errors() {
  const ConfigValidation report = powerguard::config::validate(makeValidConfig());
  TEST_ASSERT_TRUE_MESSAGE(report.ok(), report.errorCount > 0
                                            ? powerguard::config::describe(report.errors[0])
                                            : "unexpected failure");
}

void test_config_rejects_missing_max_expected_current() {
  Config cfg = makeValidConfig();
  cfg.sensor.maxExpectedCurrentA = 0.0f;  // HARDWARE_CONFIGURATION_REQUIRED
  const ConfigValidation report = powerguard::config::validate(cfg);
  TEST_ASSERT_FALSE(report.ok());
  TEST_ASSERT_TRUE(hasError(report, ConfigError::kMaxExpectedCurrentMissing));
}

void test_config_rejects_current_beyond_shunt_full_scale() {
  Config cfg = makeValidConfig();
  cfg.sensor.maxExpectedCurrentA = 9.0f;  // 9 A * 0.01 ohm > 81.90 mV
  const ConfigValidation report = powerguard::config::validate(cfg);
  TEST_ASSERT_TRUE(hasError(report, ConfigError::kMaxExpectedCurrentUnmeasurable));
}

void test_config_rejects_telemetry_faster_than_sampling() {
  Config cfg = makeValidConfig();
  cfg.timing.telemetryIntervalMs = 500UL;
  TEST_ASSERT_TRUE(
      hasError(powerguard::config::validate(cfg), ConfigError::kTelemetryFasterThanSample));
}

void test_config_requires_mqtt_username_and_password() {
  Config cfg = makeValidConfig();
  cfg.mqtt.password = "";
  TEST_ASSERT_TRUE(
      hasError(powerguard::config::validate(cfg), ConfigError::kMqttCredentialsMissing));
  cfg.mqtt.password = "pw";
  cfg.mqtt.username = "";
  TEST_ASSERT_TRUE(
      hasError(powerguard::config::validate(cfg), ConfigError::kMqttCredentialsMissing));
  cfg.mqtt.allowAnonymous = true;
  TEST_ASSERT_FALSE(
      hasError(powerguard::config::validate(cfg), ConfigError::kMqttCredentialsMissing));
}

void test_config_reports_missing_secrets_file() {
  Config cfg = makeValidConfig();
  cfg.secretsFilePresent = false;
  TEST_ASSERT_TRUE(hasError(powerguard::config::validate(cfg), ConfigError::kSecretsFileMissing));
}

void test_device_id_grammar() {
  TEST_ASSERT_TRUE(powerguard::config::isValidDeviceId("powerguard-01"));
  TEST_ASSERT_TRUE(powerguard::config::isValidDeviceId("a"));
  TEST_ASSERT_TRUE(powerguard::config::isValidDeviceId("0ab_c-d"));
  TEST_ASSERT_FALSE(powerguard::config::isValidDeviceId(""));
  TEST_ASSERT_FALSE(powerguard::config::isValidDeviceId("-leading"));
  TEST_ASSERT_FALSE(powerguard::config::isValidDeviceId("_leading"));
  TEST_ASSERT_FALSE(powerguard::config::isValidDeviceId("Upper"));
  TEST_ASSERT_FALSE(powerguard::config::isValidDeviceId("has space"));
  TEST_ASSERT_FALSE(
      powerguard::config::isValidDeviceId("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));  // 33 chars
}

void test_semver_grammar() {
  const char* good[] = {"0.1.0",     "1.0.0",       "10.20.30",  "1.2.3-rc1",
                        "1.2.3-rc.1", "1.2.3-0.alpha", "1.2.3-x-y", "1.2.3+001",
                        "1.2.3+build7", "1.2.3-rc.1+b.2"};
  for (const char* v : good) {
    TEST_ASSERT_TRUE_MESSAGE(powerguard::config::isValidSemVer(v), v);
  }
  const char* bad[] = {"",         "1.0",        "1.0.0.0",  "01.0.0",   "v1.0.0",
                       "1.2.3-",   "1.2.x",      "1.2.3 ",   "1.2.3-01", "1.2.3-a..b",
                       "1.2.3-a_b", "1.2.3+",    "1.2.3-rc+", "1.2.3+b..c", "1.2.3-rc.01",
                       "1.2.3++b", "1.2.3-rc!",
                       "1.2.3-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"};  // 34 chars
  for (const char* v : bad) {
    TEST_ASSERT_FALSE_MESSAGE(powerguard::config::isValidSemVer(v), v);
  }
}

void test_max_measurable_current_derives_from_shunt() {
  Config cfg = makeValidConfig();
  const float limit = powerguard::config::maxMeasurableCurrentA(cfg.sensor);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 8.190f, limit);  // 81.90 mV / 0.01 ohm
  cfg.sensor.shuntResistanceOhm = 0.0f;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, powerguard::config::maxMeasurableCurrentA(cfg.sensor));
}

// ---------------------------------------------------------------------------
// measurement validation
// ---------------------------------------------------------------------------

void test_measurement_accepts_plausible_values() {
  TEST_ASSERT_EQUAL(MeasurementStatus::kOk, powerguard::measurement::validateElectricalValues(
                                                7.42f, 0.31f, 2.30f, 8.4f, 8.19f));
}

void test_measurement_preserves_reverse_current() {
  TEST_ASSERT_EQUAL(MeasurementStatus::kOk, powerguard::measurement::validateElectricalValues(
                                                7.42f, -0.31f, -2.30f, 8.4f, 8.19f));
}

void test_measurement_rejects_out_of_envelope_values() {
  TEST_ASSERT_EQUAL(MeasurementStatus::kBusVoltageOutOfRange,
                    powerguard::measurement::validateElectricalValues(9.0f, 0.3f, 2.7f, 8.4f, 8.19f));
  TEST_ASSERT_EQUAL(MeasurementStatus::kBusVoltageOutOfRange,
                    powerguard::measurement::validateElectricalValues(-0.1f, 0.3f, 0.0f, 8.4f, 8.19f));
  TEST_ASSERT_EQUAL(MeasurementStatus::kCurrentOutOfRange,
                    powerguard::measurement::validateElectricalValues(7.4f, 9.0f, 66.6f, 8.4f, 8.19f));
  TEST_ASSERT_EQUAL(MeasurementStatus::kCurrentOutOfRange,
                    powerguard::measurement::validateElectricalValues(7.4f, -9.0f, -66.6f, 8.4f, 8.19f));
}

void test_measurement_rejects_non_finite_values() {
  const float nan = 0.0f / 0.0f;
  const float inf = 1.0f / 0.0f;
  TEST_ASSERT_EQUAL(MeasurementStatus::kBusVoltageInvalid,
                    powerguard::measurement::validateElectricalValues(nan, 0.3f, 1.0f, 8.4f, 8.19f));
  TEST_ASSERT_EQUAL(MeasurementStatus::kCurrentInvalid,
                    powerguard::measurement::validateElectricalValues(7.4f, nan, 1.0f, 8.4f, 8.19f));
  TEST_ASSERT_EQUAL(MeasurementStatus::kPowerInvalid,
                    powerguard::measurement::validateElectricalValues(7.4f, 0.3f, inf, 8.4f, 8.19f));
}

// ---------------------------------------------------------------------------
// energy integration
// ---------------------------------------------------------------------------

void test_energy_integrates_consecutive_valid_samples() {
  EnergyIntegrator energy;
  for (uint32_t k = 0; k <= 10; ++k) {
    energy.update(makeSample(k + 1, k * 1000UL, 10.0f, 1.0f));  // 10 W
  }
  // 10 intervals of 1 s at 10 W = 100 Ws = 0.027778 Wh
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 10.0f * 10.0f / 3600.0f, energy.energyWh());
}

void test_energy_breaks_continuity_across_invalid_samples() {
  EnergyIntegrator energy;
  for (uint32_t k = 0; k <= 10; ++k) {
    energy.update(makeSample(k + 1, k * 1000UL, 10.0f, 1.0f));
  }
  for (uint32_t k = 11; k <= 20; ++k) {
    energy.update(makeSample(k + 1, k * 1000UL, 0.0f, 0.0f, false));  // unmeasured gap
  }
  for (uint32_t k = 21; k <= 30; ++k) {
    energy.update(makeSample(k + 1, k * 1000UL, 10.0f, 1.0f));
  }
  // 10 intervals before the gap + 9 after it; the 10 s gap contributes nothing.
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 19.0f * 10.0f / 3600.0f, energy.energyWh());
  TEST_ASSERT_EQUAL_UINT32(1, energy.gapCount());
  TEST_ASSERT_TRUE(energy.energyWh() < 30.0f * 10.0f / 3600.0f);
}

void test_energy_never_goes_negative() {
  EnergyIntegrator energy;
  for (uint32_t k = 0; k <= 10; ++k) {
    energy.update(makeSample(k + 1, k * 1000UL, 10.0f, -5.0f));  // reverse flow
  }
  TEST_ASSERT_EQUAL_FLOAT(0.0f, energy.energyWh());
}

void test_energy_is_rollover_safe() {
  EnergyIntegrator energy;
  const uint32_t base = 0xFFFFFFFFUL - 2000UL;
  for (uint32_t k = 0; k <= 4; ++k) {
    energy.update(makeSample(k + 1, base + k * 1000UL, 10.0f, 1.0f));
  }
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 4.0f * 10.0f / 3600.0f, energy.energyWh());
}

// ---------------------------------------------------------------------------
// serial formatting
// ---------------------------------------------------------------------------

void test_serial_line_matches_canonical_format() {
  char line[powerguard::diagnostics::kMeasurementLineSize];
  const size_t n = powerguard::diagnostics::formatMeasurementLine(
      line, sizeof(line), makeSample(1, 0, 7.42f, 0.312f));
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_EQUAL_STRING("7.42 V - 0.31 A - 2.32 W", line);
}

void test_serial_line_preserves_negative_sign() {
  char line[powerguard::diagnostics::kMeasurementLineSize];
  powerguard::diagnostics::formatMeasurementLine(line, sizeof(line),
                                                 makeSample(1, 0, 7.42f, -0.312f));
  TEST_ASSERT_EQUAL_STRING("7.42 V - -0.31 A - -2.32 W", line);
}

void test_serial_line_refuses_truncation() {
  char line[8];
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::diagnostics::formatMeasurementLine(
                                  line, sizeof(line), makeSample(1, 0, 7.42f, 0.312f)));
  TEST_ASSERT_EQUAL_STRING("", line);
}

void test_reporter_prints_valid_samples_and_rate_limits_errors() {
  StringPrint out;
  powerguard::diagnostics::SampleReporter reporter(out);

  reporter.report(makeSample(1, 0, 7.42f, 0.312f), powerguard::sensor::SensorState::kReady,
                  powerguard::sensor::SensorError::kNone, 0);
  TEST_ASSERT_TRUE(out.contains("7.42 V - 0.31 A - 2.32 W"));

  // Same failure once per second for 25 s: printed at 0, 10000 and 20000 ms.
  out.clear();
  for (uint32_t t = 0; t < 25000; t += 1000) {
    reporter.report(makeSample(t / 1000 + 2, t, 0.0f, 0.0f, false),
                    powerguard::sensor::SensorState::kNotFound,
                    powerguard::sensor::SensorError::kNoI2cResponse, t);
  }
  TEST_ASSERT_EQUAL_UINT32(3, out.count("invalid sample"));
  TEST_ASSERT_TRUE(out.contains("suppressed"));
  TEST_ASSERT_EQUAL_UINT32(25, reporter.invalidCount());

  // A valid sample ends the episode, so the next failure prints immediately.
  out.clear();
  reporter.report(makeSample(100, 26000, 7.42f, 0.312f), powerguard::sensor::SensorState::kReady,
                  powerguard::sensor::SensorError::kNone, 26000);
  reporter.report(makeSample(101, 27000, 0.0f, 0.0f, false),
                  powerguard::sensor::SensorState::kNotFound,
                  powerguard::sensor::SensorError::kNoI2cResponse, 27000);
  TEST_ASSERT_EQUAL_UINT32(1, out.count("invalid sample"));
}

// ---------------------------------------------------------------------------
// telemetry payload contract
// ---------------------------------------------------------------------------

void test_telemetry_payload_matches_mqtt_spec() {
  TelemetrySample item;
  item.sample = makeSample(42, 1000, 7.84f, 0.417f);
  item.energyWh = 0.284f;
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  const size_t n = powerguard::telemetry::formatTelemetryJson(payload, sizeof(payload), item,
                                                              "7fa31c09", "0.1.0", nullptr);
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_TRUE(n < 2048);
  TEST_ASSERT_EQUAL_STRING(
      "{\"schema_version\":1,\"boot_id\":\"7fa31c09\",\"seq\":42,\"sampled_at\":null,"
      "\"voltage_v\":7.840,\"current_a\":0.417,\"power_w\":3.269,\"energy_wh\":0.284000,"
      "\"sensor_status\":\"ok\",\"firmware_version\":\"0.1.0\"}",
      payload);
  // device_id belongs to the topic, never the payload.
  TEST_ASSERT_NULL(strstr(payload, "device_id"));
}

void test_telemetry_payload_accepts_rfc3339_timestamp() {
  TelemetrySample item;
  item.sample = makeSample(7, 1000, 7.0f, 0.5f);
  item.energyWh = 0.0f;
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  powerguard::telemetry::formatTelemetryJson(payload, sizeof(payload), item, "7fa31c09", "0.1.0",
                                             "2026-09-21T01:02:03.250Z");
  TEST_ASSERT_NOT_NULL(strstr(payload, "\"sampled_at\":\"2026-09-21T01:02:03.250Z\""));
}

void test_telemetry_refuses_invalid_sample() {
  TelemetrySample item;
  item.sample = makeSample(43, 1000, 0.0f, 0.0f, false);
  item.energyWh = 0.1f;
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::telemetry::formatTelemetryJson(
                                  payload, sizeof(payload), item, "7fa31c09", "0.1.0", nullptr));
  TEST_ASSERT_EQUAL_STRING("", payload);
}

void test_telemetry_refuses_bad_energy() {
  TelemetrySample item;
  item.sample = makeSample(44, 1000, 7.0f, 0.5f);
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  item.energyWh = -1.0f;
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::telemetry::formatTelemetryJson(
                                  payload, sizeof(payload), item, "7fa31c09", "0.1.0", nullptr));
  item.energyWh = 0.0f / 0.0f;
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::telemetry::formatTelemetryJson(
                                  payload, sizeof(payload), item, "7fa31c09", "0.1.0", nullptr));
  item.energyWh = 1.0f / 0.0f;
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::telemetry::formatTelemetryJson(
                                  payload, sizeof(payload), item, "7fa31c09", "0.1.0", nullptr));
}

void test_telemetry_refuses_truncation() {
  TelemetrySample item;
  item.sample = makeSample(45, 1000, 7.0f, 0.5f);
  item.energyWh = 0.1f;
  char payload[64];
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::telemetry::formatTelemetryJson(
                                  payload, sizeof(payload), item, "7fa31c09", "0.1.0", nullptr));
  TEST_ASSERT_EQUAL_STRING("", payload);
}

void test_status_payload_contract() {
  char payload[powerguard::telemetry::kStatusJsonSize];
  TEST_ASSERT_TRUE(powerguard::telemetry::formatStatusJson(payload, sizeof(payload), "online",
                                                           "7fa31c09", "0.1.0") > 0);
  TEST_ASSERT_EQUAL_STRING(
      "{\"schema_version\":1,\"status\":\"online\",\"boot_id\":\"7fa31c09\","
      "\"firmware_version\":\"0.1.0\"}",
      payload);
  TEST_ASSERT_TRUE(powerguard::telemetry::formatStatusJson(payload, sizeof(payload), "offline",
                                                           "7fa31c09", "0.1.0") > 0);
  TEST_ASSERT_EQUAL_UINT32(0, powerguard::telemetry::formatStatusJson(payload, sizeof(payload),
                                                                      "weird", "7fa31c09", "0.1.0"));
  TEST_ASSERT_EQUAL_UINT32(
      0, powerguard::telemetry::formatStatusJson(payload, 16, "online", "7fa31c09", "0.1.0"));
}

void test_boot_id_is_eight_lowercase_hex() {
  char bootId[powerguard::telemetry::kBootIdLength + 1] = {0};
  powerguard::telemetry::generateBootId(bootId, sizeof(bootId));
  TEST_ASSERT_EQUAL_UINT32(8, strlen(bootId));
  for (size_t i = 0; i < 8; ++i) {
    const char c = bootId[i];
    TEST_ASSERT_TRUE((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));
  }
  TEST_ASSERT_EQUAL_STRING("7fa31c09", bootId);  // deterministic stub RNG
}

// ---------------------------------------------------------------------------
// bounded queue
// ---------------------------------------------------------------------------

void test_queue_fifo_order_and_overflow_drops_oldest() {
  TelemetryQueue queue;
  const size_t capacity = TelemetryQueue::capacity();
  TEST_ASSERT_TRUE(queue.empty());

  for (uint32_t i = 0; i < capacity + 9; ++i) {
    TelemetrySample item;
    item.sample = makeSample(i + 1, i * 1000UL, 7.0f, 0.5f);
    item.energyWh = 0.0f;
    queue.push(item);
  }
  TEST_ASSERT_EQUAL_UINT32(capacity, queue.size());
  TEST_ASSERT_EQUAL_UINT32(9, queue.droppedCount());

  // The 9 oldest were dropped, so the head is seq 10.
  TelemetrySample out;
  TEST_ASSERT_TRUE(queue.pop(out));
  TEST_ASSERT_EQUAL_UINT32(10, out.sample.seq);

  uint32_t expected = 11;
  while (queue.pop(out)) {
    TEST_ASSERT_EQUAL_UINT32(expected, out.sample.seq);
    ++expected;
  }
  TEST_ASSERT_TRUE(queue.empty());
  TEST_ASSERT_FALSE(queue.pop(out));
}

// ---------------------------------------------------------------------------
// reconnect backoff
// ---------------------------------------------------------------------------

void test_backoff_jitter_is_bounded_and_capped() {
  // No jitter configured: the window is exactly the base.
  TEST_ASSERT_EQUAL_UINT32(1000, Backoff::withJitter(1000, 60000, 0, 0xFFFFFFFFUL));
  // 20 % jitter never shortens the window and never exceeds the cap.
  TEST_ASSERT_EQUAL_UINT32(1000, Backoff::withJitter(1000, 60000, 20, 0));
  TEST_ASSERT_EQUAL_UINT32(1200, Backoff::withJitter(1000, 60000, 20, 200));
  TEST_ASSERT_TRUE(Backoff::withJitter(1000, 60000, 20, 0xFFFFFFFFUL) <= 1200);
  TEST_ASSERT_EQUAL_UINT32(60000, Backoff::withJitter(60000, 60000, 20, 0xFFFFFFFFUL));
}

void test_backoff_first_failure_uses_initial_window() {
  test_set_random(0);  // deterministic: no jitter
  Backoff backoff(1000, 60000, 20);
  const uint32_t expected[] = {1000, 2000, 4000, 8000, 16000, 32000, 60000, 60000};
  for (uint32_t step : expected) {
    backoff.armAfterFailure(0);
    TEST_ASSERT_EQUAL_UINT32(step, backoff.baseDelayMs());
  }
  // A success resets both the window and the failure flag.
  backoff.reset();
  backoff.armAfterFailure(0);
  TEST_ASSERT_EQUAL_UINT32(1000, backoff.baseDelayMs());
}

void test_backoff_expiry_is_rollover_safe() {
  test_set_random(0);
  Backoff backoff(1000, 60000, 20);
  const uint32_t near_wrap = 0xFFFFFFFFUL - 500UL;
  backoff.armAfterFailure(near_wrap);
  TEST_ASSERT_FALSE(backoff.expired(near_wrap));
  TEST_ASSERT_FALSE(backoff.expired(near_wrap + 999UL));  // wraps past zero
  TEST_ASSERT_TRUE(backoff.expired(near_wrap + 1000UL));
}

void test_backoff_is_not_expired_before_arming() {
  Backoff backoff(1000, 60000, 20);
  TEST_ASSERT_FALSE(backoff.armed());
  TEST_ASSERT_FALSE(backoff.expired(100000));
}


// ---------------------------------------------------------------------------
// telemetry payload parsed as JSON (field set, order, types, values)
// ---------------------------------------------------------------------------

void test_telemetry_payload_parses_to_exact_v1_field_set() {
  TelemetrySample item;
  item.sample = makeSample(42, 1000, 7.84f, 0.417f);
  item.energyWh = 0.284f;
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  TEST_ASSERT_TRUE(powerguard::telemetry::formatTelemetryJson(payload, sizeof(payload), item,
                                                              "7fa31c09", "0.1.0", nullptr) > 0);

  std::vector<JsonField> fields;
  TEST_ASSERT_TRUE_MESSAGE(parseFlatJson(payload, fields), payload);

  const char* expected[] = {"schema_version", "boot_id",   "seq",     "sampled_at",
                            "voltage_v",      "current_a", "power_w", "energy_wh",
                            "sensor_status",  "firmware_version"};
  const size_t expectedCount = sizeof(expected) / sizeof(expected[0]);
  TEST_ASSERT_EQUAL_UINT32(expectedCount, fields.size());  // no unknown fields
  for (size_t i = 0; i < expectedCount; ++i) {
    TEST_ASSERT_EQUAL_STRING(expected[i], fields[i].key.c_str());
  }

  TEST_ASSERT_TRUE(isJsonNumber(findField(fields, "schema_version")->raw));
  TEST_ASSERT_EQUAL_STRING("1", findField(fields, "schema_version")->raw.c_str());

  const JsonField* bootId = findField(fields, "boot_id");
  TEST_ASSERT_TRUE(isJsonString(bootId->raw));
  const std::string boot = jsonStringValue(bootId->raw);
  TEST_ASSERT_EQUAL_UINT32(8, boot.size());
  for (char c : boot) {
    TEST_ASSERT_TRUE((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));
  }

  TEST_ASSERT_TRUE(isJsonNumber(findField(fields, "seq")->raw));
  TEST_ASSERT_EQUAL_STRING("42", findField(fields, "seq")->raw.c_str());
  TEST_ASSERT_TRUE(isJsonNull(findField(fields, "sampled_at")->raw));

  const char* numeric[] = {"voltage_v", "current_a", "power_w", "energy_wh"};
  for (const char* key : numeric) {
    TEST_ASSERT_TRUE_MESSAGE(isJsonNumber(findField(fields, key)->raw), key);
  }
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 7.840f,
                           strtof(findField(fields, "voltage_v")->raw.c_str(), nullptr));
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 0.417f,
                           strtof(findField(fields, "current_a")->raw.c_str(), nullptr));
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 7.84f * 0.417f,
                           strtof(findField(fields, "power_w")->raw.c_str(), nullptr));
  TEST_ASSERT_TRUE(strtof(findField(fields, "energy_wh")->raw.c_str(), nullptr) >= 0.0f);

  const JsonField* status = findField(fields, "sensor_status");
  TEST_ASSERT_TRUE(isJsonString(status->raw));
  TEST_ASSERT_EQUAL_STRING("ok", jsonStringValue(status->raw).c_str());

  const JsonField* version = findField(fields, "firmware_version");
  TEST_ASSERT_TRUE(isJsonString(version->raw));
  TEST_ASSERT_TRUE(jsonStringValue(version->raw).size() <= 32);

  TEST_ASSERT_NULL(findField(fields, "device_id"));
}

void test_telemetry_payload_keeps_signs_and_parses_as_numbers() {
  TelemetrySample item;
  item.sample = makeSample(43, 1000, 7.84f, -0.417f);
  item.energyWh = 0.0f;
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  powerguard::telemetry::formatTelemetryJson(payload, sizeof(payload), item, "7fa31c09", "0.1.0",
                                             nullptr);
  std::vector<JsonField> fields;
  TEST_ASSERT_TRUE(parseFlatJson(payload, fields));
  TEST_ASSERT_TRUE(isJsonNumber(findField(fields, "current_a")->raw));
  TEST_ASSERT_TRUE(isJsonNumber(findField(fields, "power_w")->raw));
  TEST_ASSERT_TRUE(strtof(findField(fields, "current_a")->raw.c_str(), nullptr) < 0.0f);
  TEST_ASSERT_TRUE(strtof(findField(fields, "power_w")->raw.c_str(), nullptr) < 0.0f);
}

void test_status_payload_parses_to_exact_v1_field_set() {
  char payload[powerguard::telemetry::kStatusJsonSize];
  powerguard::telemetry::formatStatusJson(payload, sizeof(payload), "online", "7fa31c09", "0.1.0");
  std::vector<JsonField> fields;
  TEST_ASSERT_TRUE_MESSAGE(parseFlatJson(payload, fields), payload);

  const char* expected[] = {"schema_version", "status", "boot_id", "firmware_version"};
  TEST_ASSERT_EQUAL_UINT32(4, fields.size());  // no unknown fields
  for (size_t i = 0; i < 4; ++i) {
    TEST_ASSERT_EQUAL_STRING(expected[i], fields[i].key.c_str());
  }

  TEST_ASSERT_TRUE(isJsonNumber(findField(fields, "schema_version")->raw));
  TEST_ASSERT_EQUAL_STRING("1", findField(fields, "schema_version")->raw.c_str());

  const JsonField* status = findField(fields, "status");
  TEST_ASSERT_TRUE(isJsonString(status->raw));
  TEST_ASSERT_EQUAL_STRING("online", jsonStringValue(status->raw).c_str());

  const JsonField* bootId = findField(fields, "boot_id");
  TEST_ASSERT_TRUE(isJsonString(bootId->raw));
  const std::string boot = jsonStringValue(bootId->raw);
  TEST_ASSERT_EQUAL_UINT32(8, boot.size());
  for (char c : boot) {
    TEST_ASSERT_TRUE((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));
  }

  const JsonField* version = findField(fields, "firmware_version");
  TEST_ASSERT_TRUE(isJsonString(version->raw));
  const std::string semver = jsonStringValue(version->raw);
  TEST_ASSERT_TRUE(semver.size() <= 32);
  TEST_ASSERT_TRUE(powerguard::config::isValidSemVer(semver.c_str()));

  TEST_ASSERT_NULL(findField(fields, "device_id"));

  // The offline variant carries the same shape.
  powerguard::telemetry::formatStatusJson(payload, sizeof(payload), "offline", "7fa31c09", "0.1.0");
  TEST_ASSERT_TRUE(parseFlatJson(payload, fields));
  TEST_ASSERT_EQUAL_UINT32(4, fields.size());
  TEST_ASSERT_EQUAL_STRING("offline", jsonStringValue(findField(fields, "status")->raw).c_str());
}

void test_json_reader_rejects_malformed_documents() {
  // Guards the reader itself, so a payload assertion cannot pass by accident.
  const char* malformed[] = {
      "",
      "{}",
      "{\"a\":1,}",                  // trailing comma
      "{,\"a\":1}",                  // leading comma
      "{\"a\":1 \"b\":2}",           // missing separator
      "{\"a\":1,,\"b\":2}",          // empty member
      "{\"a\":}",                    // missing value
      "{\"a\":oops}",                // bare token that is not a number or null
      "{\"a\":nul}",                 // truncated null
      "{\"a\":1.2.3}",               // malformed number
      "{\"a\":--1}",                 // malformed sign
      "{\"a\":1e3}",                 // exponent not used by v1
      "{\"a\":\"unterminated}",      // unterminated string
      "{\"\":1}",                    // empty key
      "{\"a\":1,\"a\":2}",           // duplicate key
      "{\"a\":{\"b\":1}}",           // nesting
      "{\"a\":1}trailing",           // junk after the object
      "prefix{\"a\":1}",             // junk before the object
  };
  std::vector<JsonField> fields;
  for (const char* text : malformed) {
    TEST_ASSERT_FALSE_MESSAGE(parseFlatJson(text, fields), text);
  }

  // ... and still accepts the shapes the firmware actually emits.
  TEST_ASSERT_TRUE(parseFlatJson("{\"a\":1}", fields));
  TEST_ASSERT_TRUE(parseFlatJson("{\"a\":-1.5,\"b\":null,\"c\":\"x\"}", fields));
  TEST_ASSERT_EQUAL_UINT32(3, fields.size());
}

// ---------------------------------------------------------------------------
// scheduler (production sched::Deadline used by the main loop)
// ---------------------------------------------------------------------------

void test_deadline_fires_only_after_the_interval() {
  Deadline deadline;
  deadline.start(1000, 500);
  TEST_ASSERT_FALSE(deadline.due(1000));
  TEST_ASSERT_FALSE(deadline.due(1499));
  TEST_ASSERT_TRUE(deadline.due(1500));
  TEST_ASSERT_FALSE(deadline.due(1500));  // re-anchored
  TEST_ASSERT_TRUE(deadline.due(2000));
}

void test_deadline_does_not_accumulate_catch_up_firings() {
  Deadline deadline(0, 1000);
  TEST_ASSERT_TRUE(deadline.due(5000));   // five intervals late
  TEST_ASSERT_FALSE(deadline.due(5001));  // one firing only, anchored at 5000
  TEST_ASSERT_TRUE(deadline.due(6000));
}

void test_deadline_never_fires_before_start_or_with_zero_interval() {
  Deadline unstarted;
  TEST_ASSERT_FALSE(unstarted.started());
  TEST_ASSERT_FALSE(unstarted.due(100000));

  Deadline zero(0, 0);
  TEST_ASSERT_FALSE(zero.due(100000));
}

void test_deadline_is_rollover_safe() {
  const uint32_t nearWrap = 0xFFFFFFFFUL - 400UL;
  Deadline deadline(nearWrap, 1000);
  TEST_ASSERT_FALSE(deadline.due(nearWrap + 999UL));  // wraps past zero
  TEST_ASSERT_TRUE(deadline.due(nearWrap + 1000UL));
  TEST_ASSERT_EQUAL_UINT32(500, deadline.elapsedMs(nearWrap + 1500UL));
}

void test_sample_and_telemetry_deadlines_are_independent() {
  const uint32_t start = 0xFFFFFFFFUL - 30000UL;  // also exercises rollover
  Deadline sample(start, 1000);
  Deadline telemetry(start, 2000);
  uint32_t samples = 0;
  uint32_t published = 0;
  for (uint32_t k = 1; k <= 60000UL; ++k) {
    const uint32_t now = start + k;
    if (sample.due(now)) {
      ++samples;
    }
    if (telemetry.due(now)) {
      ++published;
    }
  }
  TEST_ASSERT_EQUAL_UINT32(60, samples);
  TEST_ASSERT_EQUAL_UINT32(30, published);
}

// ---------------------------------------------------------------------------
// power sensor (production power_sensor.cpp driven through an INA226 fake)
// ---------------------------------------------------------------------------

void test_sensor_refuses_to_start_without_max_expected_current() {
  Config cfg = makeValidConfig();
  cfg.sensor.maxExpectedCurrentA = 0.0f;
  PowerSensor sensor(cfg);
  TEST_ASSERT_FALSE(sensor.begin());
  TEST_ASSERT_EQUAL(SensorState::kConfigError, sensor.state());
  TEST_ASSERT_EQUAL(SensorError::kMaxCurrentMissing, sensor.error());
  TEST_ASSERT_EQUAL_UINT32(0, wire_fake().beginCalls);  // the bus is never touched

  // A configuration error is not retryable.
  TEST_ASSERT_FALSE(sensor.tick(100000));
  TEST_ASSERT_EQUAL_UINT32(1, sensor.attemptCount());
}

void test_sensor_uses_configured_pins_address_and_calibration() {
  Config cfg = makeValidConfig();
  PowerSensor sensor(cfg);
  TEST_ASSERT_TRUE(sensor.begin());
  TEST_ASSERT_EQUAL(SensorState::kReady, sensor.state());
  TEST_ASSERT_EQUAL_INT(4, wire_fake().sdaPin);  // D2 / GPIO4
  TEST_ASSERT_EQUAL_INT(5, wire_fake().sclPin);  // D1 / GPIO5
  TEST_ASSERT_EQUAL_UINT32(100000UL, wire_fake().clockHz);
  // LSB derived from the configured max current, not a driver default.
  TEST_ASSERT_FLOAT_WITHIN(1e-9f, 5.0f / 32768.0f, sensor.currentLsbA());
  TEST_ASSERT_EQUAL_UINT16(0x5449, sensor.manufacturerId());
  TEST_ASSERT_EQUAL_UINT16(0x2260, sensor.dieId());
}

void test_sensor_reports_not_found_when_the_bus_is_silent() {
  ina226_fake().connected = false;
  PowerSensor sensor(makeValidConfig());
  TEST_ASSERT_FALSE(sensor.begin());
  TEST_ASSERT_EQUAL(SensorState::kNotFound, sensor.state());
  TEST_ASSERT_EQUAL(SensorError::kNoI2cResponse, sensor.error());
}

void test_sensor_rejects_a_foreign_device_identity() {
  ina226_fake().dieId = 0x1234;
  PowerSensor sensor(makeValidConfig());
  TEST_ASSERT_FALSE(sensor.begin());
  TEST_ASSERT_EQUAL(SensorState::kInitFailed, sensor.state());
  TEST_ASSERT_EQUAL(SensorError::kIdentityMismatch, sensor.error());

  // The check can be waived deliberately for a verified clone.
  Config cfg = makeValidConfig();
  cfg.sensor.requireIdentityCheck = false;
  PowerSensor permissive(cfg);
  TEST_ASSERT_TRUE(permissive.begin());
}

void test_sensor_reports_calibration_and_mode_failures() {
  ina226_fake().calibrationResult = INA226_ERR_SHUNTVOLTAGE_HIGH;
  PowerSensor rejected(makeValidConfig());
  TEST_ASSERT_FALSE(rejected.begin());
  TEST_ASSERT_EQUAL(SensorError::kCalibrationRejected, rejected.error());
  TEST_ASSERT_EQUAL_INT(INA226_ERR_SHUNTVOLTAGE_HIGH, rejected.driverError());

  ina226_fake_reset();
  ina226_fake().calibrationApplied = false;  // accepted but no LSB
  PowerSensor uncalibrated(makeValidConfig());
  TEST_ASSERT_FALSE(uncalibrated.begin());
  TEST_ASSERT_EQUAL(SensorError::kNotCalibrated, uncalibrated.error());

  ina226_fake_reset();
  ina226_fake().modeSetSucceeds = false;
  PowerSensor modeless(makeValidConfig());
  TEST_ASSERT_FALSE(modeless.begin());
  TEST_ASSERT_EQUAL(SensorError::kModeSetFailed, modeless.error());
}

void test_sensor_sequence_increments_on_every_attempt_including_failures() {
  PowerSensor sensor(makeValidConfig());
  TEST_ASSERT_TRUE(sensor.begin());

  test_set_millis(1000);
  const PowerMeasurement first = sensor.read();
  TEST_ASSERT_EQUAL_UINT32(1, first.seq);
  TEST_ASSERT_TRUE(first.valid);
  TEST_ASSERT_EQUAL_UINT32(1000, first.timestampMs);

  // A rejected sample still consumes a sequence number, so dropped readings
  // stay visible to the backend.
  ina226_fake().busVoltageV = 12.0f;  // outside the 8.4 V envelope
  test_set_millis(2000);
  const PowerMeasurement rejected = sensor.read();
  TEST_ASSERT_EQUAL_UINT32(2, rejected.seq);
  TEST_ASSERT_FALSE(rejected.valid);
  TEST_ASSERT_EQUAL(MeasurementStatus::kBusVoltageOutOfRange, rejected.status);

  ina226_fake().busVoltageV = 7.42f;
  test_set_millis(3000);
  const PowerMeasurement third = sensor.read();
  TEST_ASSERT_EQUAL_UINT32(3, third.seq);
  TEST_ASSERT_TRUE(third.valid);
  TEST_ASSERT_EQUAL_UINT32(3, sensor.sequence());
}

void test_sensor_invalid_read_never_overwrites_the_last_valid_sample() {
  PowerSensor sensor(makeValidConfig());
  TEST_ASSERT_TRUE(sensor.begin());
  test_set_millis(1000);
  sensor.read();
  TEST_ASSERT_TRUE(sensor.hasLastValid());
  const PowerMeasurement good = sensor.lastValid();

  ina226_fake().alertRegister = INA226_MATH_OVERFLOW_FLAG;
  test_set_millis(2000);
  const PowerMeasurement bad = sensor.read();
  TEST_ASSERT_FALSE(bad.valid);
  TEST_ASSERT_EQUAL(MeasurementStatus::kMathOverflow, bad.status);
  TEST_ASSERT_EQUAL_UINT32(good.seq, sensor.lastValid().seq);
  TEST_ASSERT_EQUAL_FLOAT(good.voltageV, sensor.lastValid().voltageV);
}

void test_sensor_transport_loss_demotes_to_retry_and_recovers() {
  PowerSensor sensor(makeValidConfig());
  TEST_ASSERT_TRUE(sensor.begin());
  test_set_millis(1000);
  sensor.read();

  // The device stops acknowledging mid-run.
  ina226_fake().connected = false;
  test_set_millis(2000);
  const PowerMeasurement lost = sensor.read();
  TEST_ASSERT_FALSE(lost.valid);
  TEST_ASSERT_EQUAL(MeasurementStatus::kTransportFailed, lost.status);
  TEST_ASSERT_EQUAL(SensorState::kNotFound, sensor.state());

  // No telemetry-grade reading is produced while it is down.
  test_set_millis(2500);
  TEST_ASSERT_EQUAL(MeasurementStatus::kSensorNotReady, sensor.read().status);

  // First retry only after the initial 1 s window, never immediately.
  TEST_ASSERT_FALSE(sensor.tick(2500));
  ina226_fake().connected = true;
  test_set_millis(3000);
  TEST_ASSERT_TRUE(sensor.tick(3000));
  TEST_ASSERT_EQUAL(SensorState::kReady, sensor.state());
}

void test_sensor_retry_schedule_widens_after_repeated_failures() {
  ina226_fake().connected = false;
  PowerSensor sensor(makeValidConfig());
  test_set_millis(0);
  TEST_ASSERT_FALSE(sensor.begin());  // failure at t0 arms the first window

  uint32_t attempts = 0;
  uint32_t lastAttemptMs = 0;
  uint32_t firstGap = 0;
  for (uint32_t now = 1; now <= 20000UL; ++now) {
    test_set_millis(now);
    if (sensor.tick(now)) {
      if (attempts == 0) {
        firstGap = now - lastAttemptMs;
      }
      lastAttemptMs = now;
      ++attempts;
    }
  }
  TEST_ASSERT_EQUAL_UINT32(1000, firstGap);  // exactly the initial window
  // 1s, 2s, 4s, 8s within 20 s: four retries, not a busy loop.
  TEST_ASSERT_EQUAL_UINT32(4, attempts);
  TEST_ASSERT_EQUAL(SensorState::kNotFound, sensor.state());
}

// ---------------------------------------------------------------------------

void setUp() {
  test_set_millis(0);
  test_set_random(0);
  ina226_fake_reset();
}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();

  RUN_TEST(test_config_valid_baseline_has_no_errors);
  RUN_TEST(test_config_rejects_missing_max_expected_current);
  RUN_TEST(test_config_rejects_current_beyond_shunt_full_scale);
  RUN_TEST(test_config_rejects_telemetry_faster_than_sampling);
  RUN_TEST(test_config_requires_mqtt_username_and_password);
  RUN_TEST(test_config_reports_missing_secrets_file);
  RUN_TEST(test_device_id_grammar);
  RUN_TEST(test_semver_grammar);
  RUN_TEST(test_max_measurable_current_derives_from_shunt);

  RUN_TEST(test_measurement_accepts_plausible_values);
  RUN_TEST(test_measurement_preserves_reverse_current);
  RUN_TEST(test_measurement_rejects_out_of_envelope_values);
  RUN_TEST(test_measurement_rejects_non_finite_values);

  RUN_TEST(test_energy_integrates_consecutive_valid_samples);
  RUN_TEST(test_energy_breaks_continuity_across_invalid_samples);
  RUN_TEST(test_energy_never_goes_negative);
  RUN_TEST(test_energy_is_rollover_safe);

  RUN_TEST(test_serial_line_matches_canonical_format);
  RUN_TEST(test_serial_line_preserves_negative_sign);
  RUN_TEST(test_serial_line_refuses_truncation);
  RUN_TEST(test_reporter_prints_valid_samples_and_rate_limits_errors);

  RUN_TEST(test_telemetry_payload_matches_mqtt_spec);
  RUN_TEST(test_telemetry_payload_accepts_rfc3339_timestamp);
  RUN_TEST(test_telemetry_refuses_invalid_sample);
  RUN_TEST(test_telemetry_refuses_bad_energy);
  RUN_TEST(test_telemetry_refuses_truncation);
  RUN_TEST(test_status_payload_contract);
  RUN_TEST(test_boot_id_is_eight_lowercase_hex);

  RUN_TEST(test_queue_fifo_order_and_overflow_drops_oldest);

  RUN_TEST(test_backoff_jitter_is_bounded_and_capped);
  RUN_TEST(test_backoff_first_failure_uses_initial_window);
  RUN_TEST(test_backoff_expiry_is_rollover_safe);
  RUN_TEST(test_backoff_is_not_expired_before_arming);

  RUN_TEST(test_telemetry_payload_parses_to_exact_v1_field_set);
  RUN_TEST(test_telemetry_payload_keeps_signs_and_parses_as_numbers);
  RUN_TEST(test_status_payload_parses_to_exact_v1_field_set);
  RUN_TEST(test_json_reader_rejects_malformed_documents);

  RUN_TEST(test_deadline_fires_only_after_the_interval);
  RUN_TEST(test_deadline_does_not_accumulate_catch_up_firings);
  RUN_TEST(test_deadline_never_fires_before_start_or_with_zero_interval);
  RUN_TEST(test_deadline_is_rollover_safe);
  RUN_TEST(test_sample_and_telemetry_deadlines_are_independent);

  RUN_TEST(test_sensor_refuses_to_start_without_max_expected_current);
  RUN_TEST(test_sensor_uses_configured_pins_address_and_calibration);
  RUN_TEST(test_sensor_reports_not_found_when_the_bus_is_silent);
  RUN_TEST(test_sensor_rejects_a_foreign_device_identity);
  RUN_TEST(test_sensor_reports_calibration_and_mode_failures);
  RUN_TEST(test_sensor_sequence_increments_on_every_attempt_including_failures);
  RUN_TEST(test_sensor_invalid_read_never_overwrites_the_last_valid_sample);
  RUN_TEST(test_sensor_transport_loss_demotes_to_retry_and_recovers);
  RUN_TEST(test_sensor_retry_schedule_widens_after_repeated_failures);

  return UNITY_END();
}
