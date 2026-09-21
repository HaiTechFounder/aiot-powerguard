// AIoT PowerGuard - MQTT 3.1.1 client.

#include "mqtt_manager.h"

#include <Arduino.h>
#include <stdio.h>

#include "telemetry.h"

namespace powerguard {
namespace net {
namespace {

bool isBlank(const char* value) { return value == nullptr || value[0] == '\0'; }

}  // namespace

MqttManager::MqttManager(const config::Config& cfg, Print& out)
    : cfg_(cfg),
      out_(out),
      transport_(),
      client_(transport_),
      backoff_(cfg.mqtt.reconnectIntervalMs, cfg.mqtt.reconnectMaxIntervalMs,
               POWERGUARD_BACKOFF_JITTER_PERCENT),
      bootId_(nullptr),
      telemetryTopic_(),
      statusTopic_(),
      clientId_(),
      state_(MqttState::kDisabled),
      statusPublished_(false),
      connectCount_(0),
      dropCount_(0),
      publishCount_(0),
      publishFailureCount_(0),
      willFailureCount_(0),
      lastConnectError_(0) {}

void MqttManager::begin(const char* bootId) {
  bootId_ = bootId;
  if (isBlank(cfg_.mqtt.host) || isBlank(cfg_.identity.deviceId)) {
    state_ = MqttState::kDisabled;
    return;
  }

  snprintf(telemetryTopic_, sizeof(telemetryTopic_), "powerguard/v1/devices/%s/telemetry",
           cfg_.identity.deviceId);
  snprintf(statusTopic_, sizeof(statusTopic_), "powerguard/v1/devices/%s/status",
           cfg_.identity.deviceId);
  snprintf(clientId_, sizeof(clientId_), "powerguard-device-%s", cfg_.identity.deviceId);

  client_.setId(clientId_);
  if (!isBlank(cfg_.mqtt.username)) {
    client_.setUsernamePassword(cfg_.mqtt.username, cfg_.mqtt.password);
  }
  client_.setKeepAliveInterval(cfg_.mqtt.keepAliveMs);
  client_.setConnectionTimeout(cfg_.mqtt.connectTimeoutMs);
  client_.setTxPayloadSize(telemetry::kTelemetryJsonSize);

  state_ = MqttState::kWaitingForWifi;
  backoff_.reset();
}

void MqttManager::enterIdle(uint32_t nowMs, bool afterFailedAttempt) {
  state_ = MqttState::kIdle;
  statusPublished_ = false;
  if (afterFailedAttempt) {
    // First failure since the last success uses the initial window.
    backoff_.armAfterFailure(nowMs);
  } else {
    backoff_.reset();
    backoff_.arm(nowMs);
  }
}

bool MqttManager::publishStatus(const char* status) {
  char payload[telemetry::kStatusJsonSize];
  const size_t length = telemetry::formatStatusJson(payload, sizeof(payload), status, bootId_,
                                                    cfg_.identity.firmwareVersion);
  if (length == 0) {
    return false;
  }
  // Status is retained so the backend sees the last known state (MQTT_SPEC).
  if (client_.beginMessage(statusTopic_, length, true, 1) == 0) {
    return false;
  }
  client_.write(reinterpret_cast<const uint8_t*>(payload), length);
  return client_.endMessage() != 0;
}

void MqttManager::attemptConnect(uint32_t nowMs) {
  // The retained offline LWT is mandatory: without it the backend would never
  // learn about an ungraceful disconnect. If it cannot be registered we refuse
  // to CONNECT at all rather than run a session with no will.
  char willPayload[telemetry::kStatusJsonSize];
  const size_t willLength = telemetry::formatStatusJson(willPayload, sizeof(willPayload), "offline",
                                                        bootId_, cfg_.identity.firmwareVersion);
  bool willRegistered = false;
  if (willLength > 0) {
    // ArduinoMqttClient 0.1.8 beginWill() always returns 0, success included, so
    // it carries no status. Registration succeeded only if the whole payload was
    // buffered and endWill() reported success. endWill() is always called once
    // begun, because it is what clears the client's will-writing state.
    client_.beginWill(statusTopic_, static_cast<unsigned short>(willLength), true, 1);
    const size_t willWritten =
        client_.write(reinterpret_cast<const uint8_t*>(willPayload), willLength);
    const int willEnded = client_.endWill();
    willRegistered = (willWritten == willLength) && (willEnded != 0);
  }
  if (!willRegistered) {
    ++willFailureCount_;
    out_.print(F("[mqtt] LWT registration failed - connect refused, retry in "));
    out_.print(backoff_.delayMs());
    out_.println(F(" ms"));
    client_.stop();
    enterIdle(nowMs, true);
    return;
  }

  // Bounded by setConnectionTimeout(); this is the only place the loop waits.
  if (client_.connect(cfg_.mqtt.host, cfg_.mqtt.port) == 0) {
    lastConnectError_ = client_.connectError();
    out_.print(F("[mqtt] connect failed err="));
    out_.print(lastConnectError_);
    out_.print(F(" retry in "));
    out_.print(backoff_.delayMs());
    out_.println(F(" ms"));
    client_.stop();
    enterIdle(nowMs, true);
    return;
  }

  lastConnectError_ = 0;
  state_ = MqttState::kConnected;
  ++connectCount_;
  backoff_.reset();

  // Status before any telemetry, including anything queued while offline.
  statusPublished_ = publishStatus("online");
  out_.print(F("[mqtt] connected as "));
  out_.print(clientId_);
  out_.println(statusPublished_ ? F(" (online published)") : F(" (status publish failed)"));
}

void MqttManager::tick(uint32_t nowMs, bool wifiConnected) {
  if (state_ == MqttState::kDisabled) {
    return;
  }

  if (!wifiConnected) {
    if (state_ == MqttState::kConnected) {
      ++dropCount_;
      out_.println(F("[mqtt] disconnected - Wi-Fi down"));
      client_.stop();
    }
    // Hold at the initial window so reconnect is prompt once the link returns.
    state_ = MqttState::kWaitingForWifi;
    statusPublished_ = false;
    backoff_.reset();
    return;
  }

  if (state_ == MqttState::kConnected) {
    if (!client_.connected()) {
      ++dropCount_;
      out_.println(F("[mqtt] broker connection lost"));
      client_.stop();
      // A loss is not a failed connect attempt: retry from the initial window.
      enterIdle(nowMs, false);
      return;
    }
    client_.poll();  // services keep-alive and inbound packets
    if (!statusPublished_) {
      statusPublished_ = publishStatus("online");
    }
    return;
  }

  if (state_ == MqttState::kWaitingForWifi) {
    // The link just came back: attempt immediately. A backoff window is only
    // armed if this attempt fails.
    state_ = MqttState::kIdle;
    attemptConnect(nowMs);
    return;
  }

  // kIdle
  if (!backoff_.armed()) {
    backoff_.arm(nowMs);
    return;
  }
  if (backoff_.expired(nowMs)) {
    attemptConnect(nowMs);
  }
}

bool MqttManager::publishTelemetry(const char* payload, size_t length) {
  if (!isReadyForTelemetry() || payload == nullptr || length == 0) {
    return false;
  }
  // QoS 1, non-retained (MQTT_SPEC topic table).
  if (client_.beginMessage(telemetryTopic_, length, false, 1) == 0) {
    ++publishFailureCount_;
    return false;
  }
  client_.write(reinterpret_cast<const uint8_t*>(payload), length);
  if (client_.endMessage() == 0) {
    ++publishFailureCount_;
    return false;
  }
  ++publishCount_;
  return true;
}

void MqttManager::disconnectGracefully() {
  if (state_ != MqttState::kConnected) {
    return;
  }
  publishStatus("offline");
  client_.stop();
  state_ = MqttState::kIdle;
  statusPublished_ = false;
}

const char* describe(MqttState state) {
  switch (state) {
    case MqttState::kDisabled:
      return "DISABLED";
    case MqttState::kWaitingForWifi:
      return "WAITING_FOR_WIFI";
    case MqttState::kIdle:
      return "IDLE";
    case MqttState::kConnected:
      return "CONNECTED";
  }
  return "UNKNOWN";
}

}  // namespace net
}  // namespace powerguard
