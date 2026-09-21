// AIoT PowerGuard - telemetry payload and buffering (P02-T09).
//
// Builds the MQTT v1 JSON payloads with fixed buffers and snprintf; no JSON
// library. Field names, types and order follow Phase 01 MQTT_SPEC.md.
// `device_id` is carried by the topic and never duplicated into the payload.

#pragma once

#include <stddef.h>
#include <stdint.h>

#include "config.h"
#include "measurement.h"

namespace powerguard {
namespace telemetry {

// MQTT_SPEC caps the payload at 2 KiB; the v1 payload is far smaller.
constexpr size_t kTelemetryJsonSize = 256;
constexpr size_t kStatusJsonSize = 128;
constexpr size_t kBootIdLength = 8;  // ^[0-9a-f]{8}$

constexpr size_t kQueueCapacity = POWERGUARD_TELEMETRY_QUEUE_CAPACITY;

// A validated sample plus the cumulative energy at that instant. Structs are
// queued, not serialized strings (FIRMWARE_SPEC resource constraints).
struct TelemetrySample {
  measurement::PowerMeasurement sample;
  float energyWh;
};

// Random 8-character lowercase hex, generated once per boot.
void generateBootId(char* out, size_t len);

// Fixed-capacity FIFO. On overflow the oldest entry is dropped and counted.
class TelemetryQueue {
 public:
  TelemetryQueue();
  void push(const TelemetrySample& item);  // never fails; may drop the oldest
  bool pop(TelemetrySample& out);
  bool empty() const { return count_ == 0; }
  size_t size() const { return count_; }
  static size_t capacity() { return kQueueCapacity; }
  uint32_t droppedCount() const { return droppedCount_; }

 private:
  TelemetrySample items_[kQueueCapacity];
  size_t head_;
  size_t count_;
  uint32_t droppedCount_;
};

// Serializes the v1 telemetry payload. `sampledAt` is an RFC 3339 UTC string
// or nullptr, which emits JSON null (no time source yet).
// Returns bytes written, or 0 if the buffer is too small - a truncated payload
// is never emitted.
size_t formatTelemetryJson(char* out, size_t len, const TelemetrySample& item,
                           const char* bootId, const char* firmwareVersion,
                           const char* sampledAt);

// Serializes the v1 status payload. `status` must be "online" or "offline".
size_t formatStatusJson(char* out, size_t len, const char* status, const char* bootId,
                        const char* firmwareVersion);

}  // namespace telemetry
}  // namespace powerguard
