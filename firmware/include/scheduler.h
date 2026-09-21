// AIoT PowerGuard - deadline scheduling (P02-T11).
//
// The Scheduler module of FIRMWARE_SPEC: rollover-safe sample/publish/retry
// deadlines with no delay-driven control flow. Deadlines are independent of one
// another, so the sampling cadence and the telemetry cadence never interfere.
//
// Pure logic with no Arduino dependency, so the production code the main loop
// runs is exactly the code the host tests exercise.

#pragma once

#include <stdint.h>

namespace powerguard {
namespace sched {

class Deadline {
 public:
  Deadline();
  Deadline(uint32_t nowMs, uint32_t intervalMs);

  // Anchors the deadline at nowMs. Nothing is due until one interval elapses.
  void start(uint32_t nowMs, uint32_t intervalMs);

  // Re-anchors without changing the interval.
  void reset(uint32_t nowMs);

  void setIntervalMs(uint32_t intervalMs);
  uint32_t intervalMs() const { return intervalMs_; }
  bool started() const { return started_; }

  // Rollover-safe: unsigned subtraction wraps correctly across millis().
  uint32_t elapsedMs(uint32_t nowMs) const;

  // True when the interval has elapsed, and re-anchors at nowMs. Late calls do
  // not accumulate catch-up firings; one overdue deadline yields one firing.
  bool due(uint32_t nowMs);

 private:
  uint32_t lastMs_;
  uint32_t intervalMs_;
  bool started_;
};

}  // namespace sched
}  // namespace powerguard
