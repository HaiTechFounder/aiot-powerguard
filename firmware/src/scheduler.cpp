// AIoT PowerGuard - deadline scheduling.

#include "scheduler.h"

namespace powerguard {
namespace sched {

Deadline::Deadline() : lastMs_(0), intervalMs_(0), started_(false) {}

Deadline::Deadline(uint32_t nowMs, uint32_t intervalMs)
    : lastMs_(nowMs), intervalMs_(intervalMs), started_(true) {}

void Deadline::start(uint32_t nowMs, uint32_t intervalMs) {
  lastMs_ = nowMs;
  intervalMs_ = intervalMs;
  started_ = true;
}

void Deadline::reset(uint32_t nowMs) { lastMs_ = nowMs; }

void Deadline::setIntervalMs(uint32_t intervalMs) { intervalMs_ = intervalMs; }

uint32_t Deadline::elapsedMs(uint32_t nowMs) const { return nowMs - lastMs_; }

bool Deadline::due(uint32_t nowMs) {
  if (!started_ || intervalMs_ == 0) {
    return false;
  }
  if (elapsedMs(nowMs) < intervalMs_) {
    return false;
  }
  lastMs_ = nowMs;
  return true;
}

}  // namespace sched
}  // namespace powerguard
