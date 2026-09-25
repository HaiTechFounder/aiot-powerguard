# AIoT PowerGuard — Firmware

NodeMCU ESP8266 (30-pin) node that measures total system current with an INA226 and publishes
validated telemetry over MQTT.

```
INA226 --I2C--> ESP8266 --validate--> Wi-Fi --> MQTT v1 --> backend
```

## ⚠️ Safety prerequisite

**Do not energize the load until these are measured and set.** The firmware refuses to run
acquisition while `MAX_EXPECTED_CURRENT_A` is unset — that is deliberate, not a bug.

| Item | Status |
|---|---|
| `SHUNT_RESISTANCE_OHM` | ✅ confirmed `0.01` (10 mΩ installed) |
| `MAX_EXPECTED_CURRENT_A` | ❌ `HARDWARE_CONFIGURATION_REQUIRED` |
| Shunt power (I²R) rating | ❌ to validate |
| Wiring / source / load ratings | ❌ to validate |
| Warning + overcurrent thresholds | ❌ disabled until experimentally approved |

`MAX_EXPECTED_CURRENT_A` selects the INA226 **calibration range**. It is *not* an alarm threshold and
not a safe-current claim. The measurable ceiling is `81.90 mV / shunt` = **8.19 A** at 0.01 Ω; that is
a sensor limit, nothing more.

## Hardware

| Signal | NodeMCU pin | ESP8266 GPIO |
|---|---|---|
| I2C SCL | `D1` | GPIO5 |
| I2C SDA | `D2` | GPIO4 |
| INA226 VCC | `3V3` | — |
| INA226 GND | `GND` | — |
| MAX7219 DIN | `D5` | GPIO14 |
| MAX7219 LOAD / CS | `D6` | GPIO12 |
| MAX7219 CLK | `D7` | GPIO13 |

- INA226 I2C address: `0x40` (A0/A1 to GND).
- INA226 shunt is wired in the **total system current** path; `VBUS` sees the 2S 18650 rail
  (8.4 V maximum fully charged).
- Manufacturer/die ID are verified at boot (`0x5449` / `0x2260`). Clone boards that report other IDs
  can be accepted with `-DPOWERGUARD_REQUIRE_SENSOR_IDENTITY=0`, but confirm the part first.
- The MAX7219 8-digit panel is a **local readout only** (`display.cpp`, bit-banged, no library): it
  shows the newest validated sample, shows dashes once that sample is stale, and publishes nothing.
  Its formatting logic (`display_format.cpp`) is covered by the host tests; the panel itself is
  hardware evidence.
- `seq` is consumed on every sensor read (1 s) and one sample is published per telemetry deadline
  (2 s), so stored rows advance `seq` by **2**. The ML pipeline treats an advance of 1..2 as
  contiguous and anything wider as a missing reading.

## Setup

```bash
pip install -U platformio                 # PlatformIO Core 6.x
cp include/secrets_example.h include/secrets.h
$EDITOR include/secrets.h                 # Wi-Fi + broker credentials
```

`include/secrets.h` is Git-ignored and must never be committed. The project still **builds** without it
— the missing file is a configuration error, not a compile error — but the node will **not run**:
configuration validation happens before anything else, and any failure halts it in a safe idle state
where neither acquisition nor connectivity starts. That is the FIRMWARE_SPEC contract
(`Validate config → invalid → safe config error`), not a defect.

At minimum set `POWERGUARD_WIFI_SSID`, `POWERGUARD_WIFI_PASSWORD`, `POWERGUARD_MQTT_HOST`,
`POWERGUARD_MQTT_USERNAME`, `POWERGUARD_MQTT_PASSWORD`, and — only once validated —
`POWERGUARD_MAX_EXPECTED_CURRENT_A`.

## Build, flash, monitor

```bash
pio run -e nodemcuv2                      # build
pio run -e nodemcuv2 -t upload            # flash
pio device monitor -b 115200              # serial
pio check -e nodemcuv2 --skip-packages \
    --src-filters="+<src/>" --src-filters="+<include/>"   # cppcheck
```

## Tests

Host unit tests run without hardware:

```bash
pio test -e native
```

They need a host `g++` on `PATH` (MinGW-w64 on Windows). On Windows the toolchain must live in a path
**without spaces** — `ld` cannot resolve its own spec paths otherwise.

`test/stubs/` provides a small Arduino surface plus a **controllable INA226/Wire fake**, so
`power_sensor.cpp` — the real production file — is exercised through detection, identity, calibration,
acquisition, sequence numbering and retry. `wifi_manager.cpp` and `mqtt_manager.cpp` stay out of the
native build: they depend on the ESP8266 Wi-Fi stack and a live broker, so their behaviour remains
hardware and network evidence (see [`../docs/hardware-test-checklist.md`](../docs/hardware-test-checklist.md)).

The suite (60 `RUN_TEST` cases in `test/test_native/test_main.cpp` as of 2026-09-25, including the
MAX7219 formatting cases) covers configuration validation, device-ID and SemVer grammar, electrical
validation, energy integration and its gap handling, serial formatting and rate limiting, the telemetry
and status payloads parsed as JSON, the bounded queue, the backoff schedule, the deadline scheduler, and the sensor bring-up/acquisition path through the INA226 fake.

## Configuration

All non-secret defaults live in [`include/config.h`](include/config.h) behind `#ifndef` guards, so any
of them can be overridden from `secrets.h` or `build_flags`. Validation runs at boot and prints every
problem it finds, with credentials redacted to `<set>` / `<missing>`.

| Setting | Default | Notes |
|---|---|---|
| `POWERGUARD_DEVICE_ID` | `powerguard-01` | must match `^[a-z0-9][a-z0-9_-]{0,31}$` |
| `POWERGUARD_SHUNT_RESISTANCE_OHM` | `0.01f` | authoritative installed value |
| `POWERGUARD_MAX_EXPECTED_CURRENT_A` | `0.0f` | **required**; 0 keeps the sensor down |
| `POWERGUARD_BUS_VOLTAGE_MAX_V` | `8.4f` | 2S 18650 envelope |
| `POWERGUARD_SENSOR_SAMPLE_INTERVAL_MS` | `1000` | |
| `POWERGUARD_TELEMETRY_INTERVAL_MS` | `2000` | never shorter than the sample interval |
| `POWERGUARD_MQTT_PORT` | `1883` | |
| `POWERGUARD_MQTT_ALLOW_ANONYMOUS` | `0` | username **and** password required when 0 |
| `POWERGUARD_TELEMETRY_QUEUE_CAPACITY` | `16` | bounded RAM FIFO |
| `POWERGUARD_BACKOFF_JITTER_PERCENT` | `20` | added on top of each retry window |
| `POWERGUARD_WARNING_CURRENT_ENABLED` | `0` | disabled until validated |
| `POWERGUARD_OVERCURRENT_ENABLED` | `0` | disabled until validated |

## MQTT contract (v1)

| Topic | QoS | Retained |
|---|---:|---|
| `powerguard/v1/devices/{device_id}/telemetry` | 1 | no |
| `powerguard/v1/devices/{device_id}/status` | 1 | yes |

Client ID `powerguard-device-{device_id}`. A retained `offline` LWT is registered before CONNECT — if
it cannot be registered the firmware refuses to connect. After connecting it publishes retained
`online` before any queued telemetry.

```json
{"schema_version":1,"boot_id":"7fa31c09","seq":42,"sampled_at":null,
 "voltage_v":7.840,"current_a":0.417,"power_w":3.269,"energy_wh":0.284000,
 "sensor_status":"ok","firmware_version":"0.1.0"}
```

`device_id` is carried by the topic and never duplicated into the payload. `sampled_at` is `null`
until a time source exists (no NTP task in Phase 02). `power_w` is signed (`voltage_v × current_a`)
so reverse flow stays visible. Only validated samples are published; sensor faults are logged locally.

## Serial output

```
7.42 V - 0.31 A - 2.30 W
[sensor] invalid sample seq=12 status=... state=NOT_FOUND error=... (+9 suppressed)
[status] sensor=READY wifi=CONNECTED mqtt=CONNECTED queue=0/16 dropped=0 published=118 energy_wh=0.284000
```

Valid samples print the canonical `xx.xx V - xx.xx A - xx.xx W` line. Repeated identical faults are
rate-limited to one line per 10 s with a suppressed count; a change prints immediately.

## Behaviour under failure

| Failure | Response |
|---|---|
| INA226 missing or unresponsive | retry 1s → 2s → … → 60s (+jitter), no reboot, no stale readings |
| Invalid sample | not published, not queued, energy continuity broken for that gap |
| Wi-Fi down | sampling continues; reconnect from the initial window, capped 60 s |
| Broker down | sampling continues; telemetry queues (16 deep, oldest dropped) |
| Wi-Fi returns | MQTT connects immediately, then status, then queued telemetry |
| Bad configuration | every problem printed once, then safe idle; sensor, Wi-Fi and MQTT are never started |

Nothing here reboots the MCU. Every retry domain (sensor, Wi-Fi, MQTT) backs off independently.

## Module map

| File | Responsibility |
|---|---|
| `config.*` | typed settings, validation, redacted summary |
| `power_sensor.*` | INA226 bring-up, detection, acquisition, retry |
| `measurement.*` | value type, electrical validation, energy integration |
| `diagnostics.*` | serial measurement line, rate-limited errors |
| `backoff.*` | shared capped exponential backoff with jitter |
| `wifi_manager.*` | station connection state machine |
| `mqtt_manager.*` | MQTT 3.1.1 session, LWT/status, QoS 1 publish |
| `telemetry.*` | fixed-buffer JSON, boot ID, bounded queue |
| `scheduler.*` | rollover-safe deadlines for sampling, telemetry and status |
| `main.cpp` | orchestration only |

## Dependencies

Pinned in [`platformio.ini`](platformio.ini): `robtillaart/INA226@0.6.6`,
`arduino-libraries/ArduinoMqttClient@0.1.8`, platform `espressif8266@4.2.1`. JSON is built with
`snprintf` into fixed buffers — there is no JSON library on the device.
