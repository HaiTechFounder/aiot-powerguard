// AIoT PowerGuard - shared retry policy.

#include "backoff.h"

#include <Arduino.h>

namespace powerguard {
namespace net {

Backoff::Backoff(uint32_t initialMs, uint32_t maxMs, uint8_t jitterPercent)
    : initialMs_(initialMs),
      maxMs_(maxMs < initialMs ? initialMs : maxMs),
      baseMs_(initialMs),
      delayMs_(initialMs),
      armedAtMs_(0),
      jitterPercent_(jitterPercent > 100 ? 100 : jitterPercent),
      armed_(false),
      failedSinceReset_(false) {
  rollJitter();
}

uint32_t Backoff::withJitter(uint32_t baseMs, uint32_t maxMs, uint8_t jitterPercent,
                             uint32_t rnd) {
  uint32_t delay = baseMs;
  if (jitterPercent > 0 && baseMs > 0) {
    // baseMs <= 60000 and jitterPercent <= 100, so this cannot overflow 32 bits.
    const uint32_t span = (baseMs / 100U) * jitterPercent + ((baseMs % 100U) * jitterPercent) / 100U;
    if (span > 0) {
      delay = baseMs + (rnd % (span + 1U));
    }
  }
  return delay > maxMs ? maxMs : delay;
}

void Backoff::rollJitter() {
  delayMs_ = withJitter(baseMs_, maxMs_, jitterPercent_, static_cast<uint32_t>(random(0, 0x7FFFFFFF)));
}

void Backoff::reset() {
  baseMs_ = initialMs_;
  armed_ = false;
  failedSinceReset_ = false;
  rollJitter();
}

void Backoff::arm(uint32_t nowMs) {
  armedAtMs_ = nowMs;
  armed_ = true;
}

void Backoff::armAfterFailure(uint32_t nowMs) {
  if (failedSinceReset_) {
    advance();
  }
  failedSinceReset_ = true;
  arm(nowMs);
}

bool Backoff::expired(uint32_t nowMs) const {
  return armed_ && (nowMs - armedAtMs_) >= delayMs_;
}

void Backoff::advance() {
  if (baseMs_ < maxMs_) {
    const uint32_t doubled = baseMs_ * 2UL;
    baseMs_ = (doubled > maxMs_ || doubled < baseMs_) ? maxMs_ : doubled;
  }
  rollJitter();
}

}  // namespace net
}  // namespace powerguard
