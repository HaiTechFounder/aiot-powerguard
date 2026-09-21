// AIoT PowerGuard - secrets template.
//
// Copy this file to `include/secrets.h` and fill in real values.
// `include/secrets.h` is Git-ignored and must never be committed.
//
//   cp include/secrets_example.h include/secrets.h
//
// Everything here is a placeholder. Leaving a value empty makes configuration
// validation fail with a clear serial error instead of silently misbehaving.

#pragma once

// ---------------------------------------------------------------------------
// Wi-Fi
// ---------------------------------------------------------------------------
#define POWERGUARD_WIFI_SSID "your-wifi-ssid"
#define POWERGUARD_WIFI_PASSWORD "your-wifi-password"

// ---------------------------------------------------------------------------
// MQTT broker (Mosquitto on the LAN)
// ---------------------------------------------------------------------------
#define POWERGUARD_MQTT_HOST "192.168.1.10"
#define POWERGUARD_MQTT_PORT 1883
#define POWERGUARD_MQTT_USERNAME "powerguard-device"
#define POWERGUARD_MQTT_PASSWORD "your-mqtt-password"

// Set to 1 only for a trusted LAN broker that accepts anonymous clients.
// #define POWERGUARD_MQTT_ALLOW_ANONYMOUS 1

// ---------------------------------------------------------------------------
// Hardware configuration that must be validated before energizing the load
// ---------------------------------------------------------------------------
// MAX_EXPECTED_CURRENT_A selects the INA226 calibration range. It is NOT an
// alarm threshold. Validate it against the INA226 shunt-voltage capability,
// the shunt I^2R rating, wiring, source and load before any energized test.
// Until then the firmware refuses to run acquisition - this is intentional.
//
// #define POWERGUARD_MAX_EXPECTED_CURRENT_A 5.0f

// Operational thresholds stay disabled until experimentally approved (ADR-007).
// #define POWERGUARD_WARNING_CURRENT_ENABLED 1
// #define POWERGUARD_WARNING_CURRENT_A 4.0f
// #define POWERGUARD_OVERCURRENT_ENABLED 1
// #define POWERGUARD_OVERCURRENT_THRESHOLD_A 5.0f

// ---------------------------------------------------------------------------
// Optional per-node identity override (must match ^[a-z0-9][a-z0-9_-]{0,31}$)
// ---------------------------------------------------------------------------
// #define POWERGUARD_DEVICE_ID "powerguard-01"
