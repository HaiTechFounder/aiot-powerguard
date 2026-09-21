// AIoT PowerGuard - serial diagnostics.

#include "diagnostics.h"

#include <Arduino.h>
#include <stdio.h>

namespace powerguard {
namespace diagnostics {
namespace {

// Widest useful value plus sign, decimal point, two decimals and terminator.
constexpr size_t kNumberBufferSize = 16;

// dtostrf with width 0 leaves no padding and keeps a leading '-'.
void format2dp(float value, char* out) { dtostrf(value, 0, 2, out); }

}  // namespace

size_t formatMeasurementLine(char* out, size_t len,
                             const measurement::PowerMeasurement& sample) {
  if (out == nullptr || len == 0) {
    return 0;
  }
  char voltage[kNumberBufferSize];
  char current[kNumberBufferSize];
  char power[kNumberBufferSize];
  format2dp(sample.voltageV, voltage);
  format2dp(sample.currentA, current);
  format2dp(sample.powerW, power);

  const int written = snprintf(out, len, "%s V - %s A - %s W", voltage, current, power);
  if (written < 0 || static_cast<size_t>(written) >= len) {
    out[0] = '\0';  // refuse a truncated line rather than print a wrong one
    return 0;
  }
  return static_cast<size_t>(written);
}

SampleReporter::SampleReporter(Print& out)
    : out_(out),
      lastStatus_(measurement::MeasurementStatus::kOk),
      lastState_(sensor::SensorState::kNotInitialized),
      lastError_(sensor::SensorError::kNone),
      hasPrintedError_(false),
      lastErrorPrintMs_(0),
      suppressedCount_(0),
      invalidCount_(0) {}

void SampleReporter::report(const measurement::PowerMeasurement& sample,
                            sensor::SensorState state, sensor::SensorError error,
                            uint32_t nowMs) {
  if (sample.valid) {
    char line[kMeasurementLineSize];
    if (formatMeasurementLine(line, sizeof(line), sample) > 0) {
      out_.println(line);
    }
    // A good sample ends the current error episode, so the next failure prints
    // immediately instead of waiting out the repeat interval.
    hasPrintedError_ = false;
    suppressedCount_ = 0;
    return;
  }

  ++invalidCount_;

  const bool changed = !hasPrintedError_ || sample.status != lastStatus_ ||
                       state != lastState_ || error != lastError_;
  // Rollover-safe elapsed check.
  const bool intervalElapsed =
      hasPrintedError_ && (nowMs - lastErrorPrintMs_) >= kErrorRepeatIntervalMs;

  if (changed || intervalElapsed) {
    printDiagnostic(sample, state, error, nowMs);
  } else {
    ++suppressedCount_;
  }
}

void SampleReporter::printDiagnostic(const measurement::PowerMeasurement& sample,
                                     sensor::SensorState state, sensor::SensorError error,
                                     uint32_t nowMs) {
  out_.print(F("[sensor] invalid sample seq="));
  out_.print(sample.seq);
  out_.print(F(" status="));
  out_.print(measurement::describe(sample.status));
  out_.print(F(" state="));
  out_.print(sensor::describe(state));
  if (error != sensor::SensorError::kNone) {
    out_.print(F(" error="));
    out_.print(sensor::describe(error));
  }
  if (suppressedCount_ > 0) {
    out_.print(F(" (+"));
    out_.print(suppressedCount_);
    out_.print(F(" suppressed)"));
  }
  out_.println();

  lastStatus_ = sample.status;
  lastState_ = state;
  lastError_ = error;
  lastErrorPrintMs_ = nowMs;
  hasPrintedError_ = true;
  suppressedCount_ = 0;
}

}  // namespace diagnostics
}  // namespace powerguard
