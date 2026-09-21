// AIoT PowerGuard - Wi-Fi connectivity.

#include "wifi_manager.h"

#include <Arduino.h>
#include <ESP8266WiFi.h>

namespace powerguard {
namespace net {
namespace {

bool isBlank(const char* value) { return value == nullptr || value[0] == '\0'; }

}  // namespace

WifiManager::WifiManager(const config::Config& cfg, Print& out)
    : cfg_(cfg),
      out_(out),
      backoff_(cfg.wifi.reconnectIntervalMs, cfg.wifi.reconnectMaxIntervalMs,
               POWERGUARD_BACKOFF_JITTER_PERCENT),
      state_(WifiState::kDisabled),
      attemptStartedMs_(0),
      connectCount_(0),
      dropCount_(0),
      timeoutCount_(0),
      disabledLogged_(false) {}

void WifiManager::begin() {
  if (isBlank(cfg_.wifi.ssid)) {
    state_ = WifiState::kDisabled;
    return;
  }
  // Keep credentials out of flash and drive reconnection ourselves so the
  // retry policy is the one this module defines.
  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  state_ = WifiState::kIdle;
  backoff_.reset();
  backoff_.arm(static_cast<uint32_t>(millis()));
}

void WifiManager::startAttempt(uint32_t nowMs) {
  attemptStartedMs_ = nowMs;
  state_ = WifiState::kConnecting;
  // Non-blocking: begin() returns immediately, progress is polled in tick().
  WiFi.begin(cfg_.wifi.ssid, cfg_.wifi.password);
  out_.println(F("[wifi] connecting"));  // SSID deliberately not printed
}

void WifiManager::enterIdle(uint32_t nowMs) {
  state_ = WifiState::kIdle;
  // First failure since the last success waits the initial window; repeated
  // failures widen it. A link loss therefore never skips the initial window.
  backoff_.armAfterFailure(nowMs);
}

void WifiManager::tick(uint32_t nowMs) {
  if (state_ == WifiState::kDisabled) {
    if (!disabledLogged_) {
      disabledLogged_ = true;
      out_.println(F("[wifi] disabled - no SSID configured"));
    }
    return;
  }

  const bool linkUp = WiFi.status() == WL_CONNECTED;

  if (linkUp) {
    if (state_ != WifiState::kConnected) {
      state_ = WifiState::kConnected;
      ++connectCount_;
      backoff_.reset();
      out_.print(F("[wifi] connected ip="));
      out_.print(WiFi.localIP());
      out_.print(F(" rssi="));
      out_.println(WiFi.RSSI());
    }
    return;
  }

  if (state_ == WifiState::kConnected) {
    ++dropCount_;
    out_.println(F("[wifi] link lost"));
    WiFi.disconnect(false);
    // A loss is not a failed attempt: retry from the initial window.
    backoff_.reset();
    state_ = WifiState::kIdle;
    backoff_.arm(nowMs);
    return;
  }

  if (state_ == WifiState::kConnecting) {
    if ((nowMs - attemptStartedMs_) < cfg_.wifi.connectTimeoutMs) {
      return;  // still associating; sensor sampling keeps running meanwhile
    }
    ++timeoutCount_;
    out_.print(F("[wifi] attempt timed out, retry in "));
    out_.print(backoff_.delayMs());
    out_.println(F(" ms"));
    WiFi.disconnect(false);
    enterIdle(nowMs);
    return;
  }

  // kIdle
  if (!backoff_.armed()) {
    backoff_.arm(nowMs);
    return;
  }
  if (backoff_.expired(nowMs)) {
    startAttempt(nowMs);
  }
}

const char* describe(WifiState state) {
  switch (state) {
    case WifiState::kDisabled:
      return "DISABLED";
    case WifiState::kIdle:
      return "IDLE";
    case WifiState::kConnecting:
      return "CONNECTING";
    case WifiState::kConnected:
      return "CONNECTED";
  }
  return "UNKNOWN";
}

}  // namespace net
}  // namespace powerguard
