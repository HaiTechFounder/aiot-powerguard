// AIoT PowerGuard - MQTT 3.1.1 client (P02-T08).
//
// Implements the Phase 01 MQTT_SPEC v1 contract: versioned topics, QoS 1,
// retained LWT and status, and the `powerguard-device-{device_id}` client ID.
// Connect attempts are bounded by a timeout and spaced by capped backoff with
// jitter; a broker outage never reboots the node or stops sampling.

#pragma once

#include <ArduinoMqttClient.h>
#include <ESP8266WiFi.h>
#include <stddef.h>
#include <stdint.h>

#include "backoff.h"
#include "config.h"

class Print;

namespace powerguard {
namespace net {

enum class MqttState : uint8_t {
  kDisabled,        // no broker host configured
  kWaitingForWifi,  // link is down; nothing to attempt
  kIdle,            // waiting out the retry window
  kConnected,
};

// "powerguard/v1/devices/" + 32-char device id + "/telemetry" + terminator.
constexpr size_t kTopicBufferSize = 80;
constexpr size_t kClientIdBufferSize = 64;

class MqttManager {
 public:
  MqttManager(const config::Config& cfg, Print& out);

  // Builds topics, client ID and the retained offline LWT. `bootId` must
  // outlive this object.
  void begin(const char* bootId);

  // Advances the connection state machine. Never loops waiting for the broker.
  void tick(uint32_t nowMs, bool wifiConnected);

  bool isConnected() const { return state_ == MqttState::kConnected; }

  // True once the retained `online` status has been published for the current
  // session, i.e. telemetry may now be sent (MQTT_SPEC ordering rule).
  bool isReadyForTelemetry() const { return state_ == MqttState::kConnected && statusPublished_; }

  MqttState state() const { return state_; }

  // QoS 1, non-retained.
  bool publishTelemetry(const char* payload, size_t length);

  // Best-effort retained `offline` before an intentional disconnect.
  void disconnectGracefully();

  uint32_t connectCount() const { return connectCount_; }
  uint32_t dropCount() const { return dropCount_; }
  uint32_t publishCount() const { return publishCount_; }
  uint32_t publishFailureCount() const { return publishFailureCount_; }
  uint32_t willFailureCount() const { return willFailureCount_; }
  int lastConnectError() const { return lastConnectError_; }
  uint32_t retryDelayMs() const { return backoff_.delayMs(); }
  const char* telemetryTopic() const { return telemetryTopic_; }
  const char* statusTopic() const { return statusTopic_; }

 private:
  bool publishStatus(const char* status);
  void enterIdle(uint32_t nowMs, bool afterFailedAttempt);
  void attemptConnect(uint32_t nowMs);

  const config::Config& cfg_;
  Print& out_;
  WiFiClient transport_;
  MqttClient client_;
  Backoff backoff_;

  const char* bootId_;
  char telemetryTopic_[kTopicBufferSize];
  char statusTopic_[kTopicBufferSize];
  char clientId_[kClientIdBufferSize];

  MqttState state_;
  bool statusPublished_;
  uint32_t connectCount_;
  uint32_t dropCount_;
  uint32_t publishCount_;
  uint32_t publishFailureCount_;
  uint32_t willFailureCount_;
  int lastConnectError_;
};

const char* describe(MqttState state);

}  // namespace net
}  // namespace powerguard
