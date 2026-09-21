// AIoT PowerGuard - NodeMCU ESP8266 firmware entry point.
//
// Orchestration only. Each concern lives in its own module:
//   config       - typed settings and validation
//   power_sensor - INA226 bring-up and acquisition
//   measurement  - validation and energy integration
//   diagnostics  - serial presentation
//   display      - local MAX7219 readout (presentation only)
//   scheduler    - rollover-safe deadlines
//   wifi_manager / mqtt_manager - connectivity
//   telemetry    - payload, bounded queue
//
// An invalid configuration halts the node in a safe idle state: neither
// acquisition nor connectivity is started (FIRMWARE_SPEC runtime state model,
// "Validate config -> invalid -> safe config error"). Otherwise sampling runs
// independently of connectivity.

#include <Arduino.h>

#include "config.h"
#include "diagnostics.h"
#include "display.h"
#include "mqtt_manager.h"
#include "power_sensor.h"
#include "scheduler.h"
#include "telemetry.h"
#include "wifi_manager.h"

namespace {

constexpr unsigned long kSerialBaud = 115200UL;
constexpr unsigned long kSerialReadyTimeoutMs = 2000UL;
constexpr uint32_t kStatusIntervalMs = 10000UL;
constexpr uint32_t kHaltedNoticeIntervalMs = 30000UL;

// Bound the work done in a single loop pass so the watchdog stays serviced.
constexpr uint8_t kMaxPublishesPerLoop = 4;

bool g_configValid = false;
uint32_t g_lastQueuedSeq = 0;
bool g_hasQueuedSeq = false;
char g_bootId[powerguard::telemetry::kBootIdLength + 1] = {0};
powerguard::sensor::SensorState g_lastReportedState =
    powerguard::sensor::SensorState::kNotInitialized;

powerguard::sched::Deadline g_sampleDeadline;
powerguard::sched::Deadline g_telemetryDeadline;
powerguard::sched::Deadline g_statusDeadline;

// The last acquisition attempt, valid or not. The display needs to know what
// the sensor just said, which is not the same question as "what was the last
// good reading" - a stale good reading must not keep showing as if it were now.
powerguard::measurement::PowerMeasurement g_lastSample{};
bool g_hasSample = false;

void waitForSerial() {
  const unsigned long start = millis();
  while (!Serial && (millis() - start) < kSerialReadyTimeoutMs) {
    yield();
  }
}

// Constructed on first use so they bind to the validated configuration without
// depending on static initialization order across translation units.
powerguard::sensor::PowerSensor& powerSensor() {
  static powerguard::sensor::PowerSensor instance(powerguard::config::config());
  return instance;
}

powerguard::diagnostics::SampleReporter& reporter() {
  static powerguard::diagnostics::SampleReporter instance(Serial);
  return instance;
}

powerguard::net::WifiManager& wifi() {
  static powerguard::net::WifiManager instance(powerguard::config::config(), Serial);
  return instance;
}

powerguard::net::MqttManager& mqtt() {
  static powerguard::net::MqttManager instance(powerguard::config::config(), Serial);
  return instance;
}

powerguard::measurement::EnergyIntegrator& energy() {
  static powerguard::measurement::EnergyIntegrator instance;
  return instance;
}

powerguard::telemetry::TelemetryQueue& queue() {
  static powerguard::telemetry::TelemetryQueue instance;
  return instance;
}

powerguard::display::Max7219Display& panel() {
  static powerguard::display::Max7219Display instance;
  return instance;
}

// Enqueues one snapshot per telemetry deadline, and only when the sensor has
// produced a new valid sample since the previous deadline. Stale values are
// never re-sent as a new reading.
void enqueueTelemetry(powerguard::sensor::PowerSensor& sensor) {
  if (!sensor.hasLastValid()) {
    return;
  }
  const powerguard::measurement::PowerMeasurement& latest = sensor.lastValid();
  if (g_hasQueuedSeq && latest.seq == g_lastQueuedSeq) {
    return;
  }
  g_lastQueuedSeq = latest.seq;
  g_hasQueuedSeq = true;

  powerguard::telemetry::TelemetrySample item;
  item.sample = latest;
  item.energyWh = energy().energyWh();
  queue().push(item);
}

// Drains the queue while the session is ready. Publishing stops at the first
// failure so the sample stays queued for the next pass.
void drainTelemetry() {
  if (!mqtt().isReadyForTelemetry()) {
    return;
  }
  char payload[powerguard::telemetry::kTelemetryJsonSize];
  for (uint8_t sent = 0; sent < kMaxPublishesPerLoop && !queue().empty(); ++sent) {
    powerguard::telemetry::TelemetrySample item;
    if (!queue().pop(item)) {
      return;
    }
    // No time source yet, so sampled_at is JSON null per MQTT_SPEC.
    const size_t length = powerguard::telemetry::formatTelemetryJson(
        payload, sizeof(payload), item, g_bootId,
        powerguard::config::config().identity.firmwareVersion, nullptr);
    if (length == 0) {
      Serial.println(F("[telemetry] payload rejected - not published"));
      continue;  // malformed or oversized: drop rather than publish bad JSON
    }
    if (!mqtt().publishTelemetry(payload, length)) {
      queue().push(item);  // put it back and retry on a later pass
      return;
    }
  }
}

void printStatusLine(powerguard::sensor::PowerSensor& sensor) {
  Serial.print(F("[status] sensor="));
  Serial.print(powerguard::sensor::describe(sensor.state()));
  Serial.print(F(" wifi="));
  Serial.print(powerguard::net::describe(wifi().state()));
  Serial.print(F(" mqtt="));
  Serial.print(powerguard::net::describe(mqtt().state()));
  Serial.print(F(" queue="));
  Serial.print(queue().size());
  Serial.print(F("/"));
  Serial.print(powerguard::telemetry::TelemetryQueue::capacity());
  Serial.print(F(" dropped="));
  Serial.print(queue().droppedCount());
  Serial.print(F(" published="));
  Serial.print(mqtt().publishCount());
  Serial.print(F(" energy_wh="));
  Serial.println(energy().energyWh(), 6);
}

}  // namespace

void setup() {
  Serial.begin(kSerialBaud);
  waitForSerial();
  Serial.println();
  Serial.print(F("AIoT PowerGuard firmware "));
  Serial.println(F(POWERGUARD_FIRMWARE_VERSION));

  const powerguard::config::Config& cfg = powerguard::config::config();
  const powerguard::config::ConfigValidation report = powerguard::config::validate(cfg);
  g_configValid = report.ok();

  powerguard::config::printSummary(Serial, cfg);
  powerguard::config::printValidation(Serial, report);

  const uint32_t now = static_cast<uint32_t>(millis());

  if (!g_configValid) {
    // Safe config error: nothing is started. The node stays alive and keeps
    // reporting why, so the operator can fix the configuration and reflash.
    g_statusDeadline.start(now, kHaltedNoticeIntervalMs);
    Serial.println(F("[boot] HALTED - configuration invalid; acquisition and connectivity "
                     "are not started"));
    return;
  }

  powerguard::telemetry::generateBootId(g_bootId, sizeof(g_bootId));
  Serial.print(F("[boot] boot_id="));
  Serial.println(g_bootId);

  powerSensor().begin();
  powerSensor().printStatus(Serial);
  g_lastReportedState = powerSensor().state();

  // Local readout comes up before connectivity so the panel is informative
  // while Wi-Fi is still associating.
  panel().begin();

  wifi().begin();
  mqtt().begin(g_bootId);

  g_sampleDeadline.start(now, cfg.timing.sampleIntervalMs);
  g_telemetryDeadline.start(now, cfg.timing.telemetryIntervalMs);
  g_statusDeadline.start(now, kStatusIntervalMs);
  Serial.println(F("[boot] acquisition active"));
}

void loop() {
  const uint32_t now = static_cast<uint32_t>(millis());

  if (!g_configValid) {
    if (g_statusDeadline.due(now)) {
      Serial.println(F("[status] HALTED - configuration invalid"));
    }
    yield();
    return;
  }

  powerguard::sensor::PowerSensor& sensor = powerSensor();

  // Non-blocking, capped retry while the sensor is missing or failing.
  if (sensor.tick(now) && sensor.state() != g_lastReportedState) {
    g_lastReportedState = sensor.state();
    sensor.printStatus(Serial);
  }

  // Sampling deadline - runs regardless of connectivity.
  if (g_sampleDeadline.due(now)) {
    const powerguard::measurement::PowerMeasurement sample = sensor.read();
    reporter().report(sample, sensor.state(), sensor.error(), now);
    // Fed with every attempt: an invalid sample breaks integration continuity
    // so the unmeasured interval is not charged to the boot total.
    energy().update(sample);
    g_lastSample = sample;
    g_hasSample = true;
  }

  // Presentation only, and before the network work so a slow reconnect cannot
  // delay the readout. Nothing below depends on what the panel did.
  {
    const bool fresh =
        g_hasSample &&
        (now - g_lastSample.timestampMs) < powerguard::display::kSampleStaleAfterMs;
    panel().tick(now, g_lastSample, fresh);
  }

  wifi().tick(now);
  mqtt().tick(now, wifi().isConnected());

  // Telemetry deadline is independent of the sampling deadline; the sensor may
  // sample faster than telemetry is published.
  if (g_telemetryDeadline.due(now)) {
    enqueueTelemetry(sensor);
  }
  drainTelemetry();

  if (g_statusDeadline.due(now)) {
    printStatusLine(sensor);
  }

  yield();
}
