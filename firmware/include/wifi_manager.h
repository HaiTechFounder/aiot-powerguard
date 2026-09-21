// AIoT PowerGuard - Wi-Fi connectivity (P02-T07).
//
// Station-mode association driven from loop(); never blocks waiting for a
// connection and never reboots on failure. Credentials come from the ignored
// local secrets file via config and are never logged.

#pragma once

#include <stdint.h>

#include "backoff.h"
#include "config.h"

class Print;

namespace powerguard {
namespace net {

enum class WifiState : uint8_t {
  kDisabled,    // no SSID configured; nothing is attempted
  kIdle,        // waiting out the retry window
  kConnecting,  // association attempt in flight
  kConnected,
};

class WifiManager {
 public:
  WifiManager(const config::Config& cfg, Print& out);

  // Puts the radio in station mode. Does not block on association.
  void begin();

  // Advances the connection state machine. Safe to call every loop pass.
  void tick(uint32_t nowMs);

  bool isConnected() const { return state_ == WifiState::kConnected; }
  WifiState state() const { return state_; }

  uint32_t connectCount() const { return connectCount_; }
  uint32_t dropCount() const { return dropCount_; }
  uint32_t timeoutCount() const { return timeoutCount_; }
  uint32_t retryDelayMs() const { return backoff_.delayMs(); }

 private:
  void startAttempt(uint32_t nowMs);
  void enterIdle(uint32_t nowMs);

  const config::Config& cfg_;
  Print& out_;
  Backoff backoff_;

  WifiState state_;
  uint32_t attemptStartedMs_;
  uint32_t connectCount_;
  uint32_t dropCount_;
  uint32_t timeoutCount_;
  bool disabledLogged_;
};

const char* describe(WifiState state);

}  // namespace net
}  // namespace powerguard
