# Phase 02 — Hardware test checklist

Everything below is **NOT_RUN**. No NodeMCU was connected, no `secrets.h` existed and no MQTT broker
was reachable during Phase 02 development, so none of it may be reported as passing. Automated
evidence (build, static analysis, 51 host unit tests) does not substitute for any line here.

## Prerequisites

Do **not** energize the load until all of these are measured and written into `include/secrets.h`:

- [ ] `MAX_EXPECTED_CURRENT_A` validated against the INA226 shunt-voltage range, the shunt `I²R`
      rating, the wiring, the source and the load.
- [ ] Shunt power dissipation at the expected maximum current is within the part's rating.
- [ ] `WARNING_CURRENT_A` and `OVERCURRENT_THRESHOLD_A` chosen experimentally, then enabled.
- [ ] Wi-Fi credentials and MQTT broker account provisioned.

The measurable ceiling is `81.90 mV / 0.01 Ω = 8.19 A`. That is a **sensor** limit, not a safe
operating current.

## Sensor bring-up

- [ ] Boot log shows `[sensor] INA226 READY`, manufacturer `0x5449`, die `0x2260`.
- [ ] Reported `current_lsb` matches the configured shunt and `MAX_EXPECTED_CURRENT_A`.
- [ ] With the INA226 disconnected at boot: state `NOT_FOUND`, retries at roughly 1 s, 2 s, 4 s, 8 s,
      16 s, 32 s, 60 s, 60 s — no reboot, no busy loop, no telemetry.
- [ ] Removing the INA226 while running demotes the sensor to retry and suppresses telemetry.
- [ ] Reconnecting it recovers to `READY` without a reboot.

## Measurement plausibility

- [ ] Serial prints the exact pattern `xx.xx V - xx.xx A - xx.xx W`.
- [ ] Bus voltage matches a multimeter reading on the 2S pack within a few tens of mV.
- [ ] A known resistive load produces the expected current within the shunt tolerance.
- [ ] Reversing the load shows a **negative** current and a **negative** power.
- [ ] `energy_wh` grows monotonically and matches `P × t` over a measured interval.

## Endurance

- [ ] 30-minute continuous run at the default 2 s telemetry cadence.
- [ ] Free heap is stable across the run (no downward trend).
- [ ] `seq` increases monotonically with no gaps other than rejected samples.
- [ ] No watchdog reset in the serial log.

## Connectivity

- [ ] Wi-Fi connects; the SSID and password never appear in the serial log.
- [ ] Power-cycling the AP: sampling continues, the node reconnects from the initial window.
- [ ] Stopping Mosquitto: sampling continues, telemetry queues, `dropped` grows once past 16 entries.
- [ ] Restarting Mosquitto: retained `online` is published **before** queued telemetry.
- [ ] Killing the node's power: the broker shows the retained `offline` LWT on
      `powerguard/v1/devices/{device_id}/status`.

## Protocol conformance

Capture with `mosquitto_sub -h <broker> -u <user> -P <pass> -t 'powerguard/v1/devices/+/#' -v`.

- [ ] Telemetry topic, QoS 1, non-retained; status topic, QoS 1, retained.
- [ ] Payload parses as JSON, is under 2 KiB, and carries exactly the ten v1 fields.
- [ ] `device_id` appears only in the topic, never in the payload.
- [ ] `boot_id` matches `^[0-9a-f]{8}$` and changes on every reboot.
- [ ] `sampled_at` is `null` (no NTP in Phase 02).
- [ ] Redact credentials before attaching any capture to a report.

## Recording results

Write outcomes into `.ai_workspace/claude/phases/phase-02-firmware/HARDWARE_TEST_RESULTS.md` with the
date, firmware version, git revision and raw serial or `mosquitto_sub` excerpts. A test that was not
run is recorded as `NOT_RUN`, never as passing.
