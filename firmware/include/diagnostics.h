// AIoT PowerGuard - serial diagnostics (P02-T05).
//
// Owns the serial presentation: the canonical measurement line and
// rate-limited error reporting. Keeps formatting out of main.cpp and out of
// the sensor module. Never prints credentials.

#pragma once

#include <stddef.h>
#include <stdint.h>

#include "measurement.h"
#include "power_sensor.h"

class Print;

namespace powerguard {
namespace diagnostics {

// An unchanged error is repeated at most this often; a change prints at once.
constexpr uint32_t kErrorRepeatIntervalMs = 10000UL;

// Enough for "-1234.56 V - -1234.56 A - -123456.78 W" plus terminator.
constexpr size_t kMeasurementLineSize = 64;

// Writes "xx.xx V - xx.xx A - xx.xx W" (2 decimals, signs preserved) into out.
// Returns the number of characters written, or 0 if the buffer is too small.
// Pure formatting - no I/O, so it can be unit tested on the host (P02-T11).
size_t formatMeasurementLine(char* out, size_t len,
                             const measurement::PowerMeasurement& sample);

class SampleReporter {
 public:
  explicit SampleReporter(Print& out);

  // Valid sample -> canonical line, once per sample.
  // Invalid sample -> concise diagnostic, printed on change and then at most
  // once per kErrorRepeatIntervalMs with the suppressed count.
  void report(const measurement::PowerMeasurement& sample, sensor::SensorState state,
              sensor::SensorError error, uint32_t nowMs);

  // Invalid-sample messages withheld since the last diagnostic was printed.
  uint32_t suppressedCount() const { return suppressedCount_; }

  // Invalid samples seen this boot.
  uint32_t invalidCount() const { return invalidCount_; }

 private:
  void printDiagnostic(const measurement::PowerMeasurement& sample, sensor::SensorState state,
                       sensor::SensorError error, uint32_t nowMs);

  Print& out_;
  measurement::MeasurementStatus lastStatus_;
  sensor::SensorState lastState_;
  sensor::SensorError lastError_;
  bool hasPrintedError_;
  uint32_t lastErrorPrintMs_;
  uint32_t suppressedCount_;
  uint32_t invalidCount_;
};

}  // namespace diagnostics
}  // namespace powerguard
