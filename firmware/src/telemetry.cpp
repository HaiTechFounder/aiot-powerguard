// AIoT PowerGuard - telemetry payload and buffering.

#include "telemetry.h"

#include <Arduino.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

namespace powerguard {
namespace telemetry {
namespace {

constexpr size_t kNumberBufferSize = 20;

// Electrical values use 3 decimals, matching the MQTT_SPEC example payload.
constexpr uint8_t kElectricalDecimals = 3;
// Energy accrues slowly at low power, so it keeps more resolution.
constexpr uint8_t kEnergyDecimals = 6;

void formatNumber(float value, uint8_t decimals, char* out) {
  dtostrf(value, 0, decimals, out);
}

}  // namespace

void generateBootId(char* out, size_t len) {
  if (out == nullptr || len < kBootIdLength + 1) {
    return;
  }
  // Hardware RNG on the ESP8266; one 32-bit draw yields the 8 hex characters.
  const uint32_t value = RANDOM_REG32;
  static const char kHex[] = "0123456789abcdef";
  for (size_t i = 0; i < kBootIdLength; ++i) {
    const uint8_t nibble = static_cast<uint8_t>((value >> ((kBootIdLength - 1 - i) * 4)) & 0x0F);
    out[i] = kHex[nibble];
  }
  out[kBootIdLength] = '\0';
}

TelemetryQueue::TelemetryQueue() : items_(), head_(0), count_(0), droppedCount_(0) {}

void TelemetryQueue::push(const TelemetrySample& item) {
  if (count_ == kQueueCapacity) {
    head_ = (head_ + 1) % kQueueCapacity;  // drop the oldest
    --count_;
    ++droppedCount_;
  }
  items_[(head_ + count_) % kQueueCapacity] = item;
  ++count_;
}

bool TelemetryQueue::pop(TelemetrySample& out) {
  if (count_ == 0) {
    return false;
  }
  out = items_[head_];
  head_ = (head_ + 1) % kQueueCapacity;
  --count_;
  return true;
}

size_t formatTelemetryJson(char* out, size_t len, const TelemetrySample& item,
                           const char* bootId, const char* firmwareVersion,
                           const char* sampledAt) {
  if (out == nullptr || len == 0 || bootId == nullptr || firmwareVersion == nullptr) {
    return 0;
  }
  // Only validated samples become v1 telemetry; sensor faults are logged
  // locally instead (MQTT_SPEC).
  if (!item.sample.valid || item.sample.status != measurement::MeasurementStatus::kOk) {
    out[0] = '\0';
    return 0;
  }
  // MQTT_SPEC: energy_wh must be finite and non-negative. Refuse to serialize
  // a value that would violate the contract rather than emit bad telemetry.
  if (!isfinite(item.energyWh) || item.energyWh < 0.0f) {
    out[0] = '\0';
    return 0;
  }

  char voltage[kNumberBufferSize];
  char current[kNumberBufferSize];
  char power[kNumberBufferSize];
  char energy[kNumberBufferSize];
  formatNumber(item.sample.voltageV, kElectricalDecimals, voltage);
  formatNumber(item.sample.currentA, kElectricalDecimals, current);
  formatNumber(item.sample.powerW, kElectricalDecimals, power);
  formatNumber(item.energyWh, kEnergyDecimals, energy);

  int written;
  if (sampledAt == nullptr) {
    written = snprintf(out, len,
                       "{\"schema_version\":1,\"boot_id\":\"%s\",\"seq\":%lu,"
                       "\"sampled_at\":null,\"voltage_v\":%s,\"current_a\":%s,"
                       "\"power_w\":%s,\"energy_wh\":%s,\"sensor_status\":\"ok\","
                       "\"firmware_version\":\"%s\"}",
                       bootId, static_cast<unsigned long>(item.sample.seq), voltage, current,
                       power, energy, firmwareVersion);
  } else {
    written = snprintf(out, len,
                       "{\"schema_version\":1,\"boot_id\":\"%s\",\"seq\":%lu,"
                       "\"sampled_at\":\"%s\",\"voltage_v\":%s,\"current_a\":%s,"
                       "\"power_w\":%s,\"energy_wh\":%s,\"sensor_status\":\"ok\","
                       "\"firmware_version\":\"%s\"}",
                       bootId, static_cast<unsigned long>(item.sample.seq), sampledAt, voltage,
                       current, power, energy, firmwareVersion);
  }

  if (written < 0 || static_cast<size_t>(written) >= len) {
    out[0] = '\0';  // refuse to publish a truncated, invalid JSON document
    return 0;
  }
  return static_cast<size_t>(written);
}

size_t formatStatusJson(char* out, size_t len, const char* status, const char* bootId,
                        const char* firmwareVersion) {
  if (out == nullptr || len == 0 || status == nullptr || bootId == nullptr ||
      firmwareVersion == nullptr) {
    return 0;
  }
  if (strcmp(status, "online") != 0 && strcmp(status, "offline") != 0) {
    out[0] = '\0';
    return 0;
  }
  const int written = snprintf(out, len,
                               "{\"schema_version\":1,\"status\":\"%s\",\"boot_id\":\"%s\","
                               "\"firmware_version\":\"%s\"}",
                               status, bootId, firmwareVersion);
  if (written < 0 || static_cast<size_t>(written) >= len) {
    out[0] = '\0';
    return 0;
  }
  return static_cast<size_t>(written);
}

}  // namespace telemetry
}  // namespace powerguard
