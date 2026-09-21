// AIoT PowerGuard - shared retry policy (P02-T10).
//
// Capped exponential backoff with jitter, used independently by the sensor,
// Wi-Fi and MQTT recovery paths. Jitter is added on top of the base delay and
// the total is clamped to maxMs, so the first window is never shorter than the
// configured initial delay and the cap is never exceeded.

#pragma once

#include <stdint.h>

namespace powerguard {
namespace net {

class Backoff {
 public:
  Backoff(uint32_t initialMs, uint32_t maxMs, uint8_t jitterPercent);

  // Back to the initial window, disarmed.
  void reset();

  // Start the current window at nowMs.
  void arm(uint32_t nowMs);

  // Arm the window after a failed attempt. The first failure since the last
  // reset uses the initial window; every subsequent failure widens it first.
  // This keeps the initial window from being skipped after a loss event.
  void armAfterFailure(uint32_t nowMs);

  bool armed() const { return armed_; }

  // Rollover-safe: unsigned subtraction wraps correctly across millis().
  bool expired(uint32_t nowMs) const;

  // Double the base window (capped) and roll a new jitter value.
  void advance();

  uint32_t delayMs() const { return delayMs_; }
  uint32_t baseDelayMs() const { return baseMs_; }

  // Pure helper, no globals: base + (rnd % (base*pct/100 + 1)), clamped to
  // maxMs. Exposed so the schedule can be unit tested on the host (P02-T11).
  static uint32_t withJitter(uint32_t baseMs, uint32_t maxMs, uint8_t jitterPercent,
                             uint32_t rnd);

 private:
  void rollJitter();

  uint32_t initialMs_;
  uint32_t maxMs_;
  uint32_t baseMs_;
  uint32_t delayMs_;
  uint32_t armedAtMs_;
  uint8_t jitterPercent_;
  bool armed_;
  bool failedSinceReset_;
};

}  // namespace net
}  // namespace powerguard
