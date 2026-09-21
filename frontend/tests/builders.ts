/** Deterministic fixtures. No clock, no randomness, no network. */

import type {
  AnomalyDto,
  DeviceDto,
  HealthDto,
  TelemetryDto,
} from "../src/api/contract";

export const DEVICE_ID = "powerguard-01";
export const BOOT_ID = "7fa31c09";

const BASE = Date.UTC(2026, 0, 1, 0, 0, 0);

export function stamp(seconds: number): string {
  return new Date(BASE + seconds * 1000).toISOString().replace(/\.(\d{3})Z$/, ".$1Z");
}

export function telemetry(overrides: Partial<TelemetryDto> = {}): TelemetryDto {
  const id = overrides.id ?? 1;
  return {
    id,
    device_id: DEVICE_ID,
    boot_id: BOOT_ID,
    seq: id,
    sampled_at: null,
    received_at: stamp(id * 2),
    voltage_v: 7.84,
    current_a: 0.417,
    power_w: 3.269,
    energy_wh: 0.284,
    sensor_status: "ok",
    anomaly: null,
    ...overrides,
  };
}

/** `count` rows, newest first, exactly as the backend returns a page. */
export function telemetryPage(count: number, startId = 1): TelemetryDto[] {
  const rows = Array.from({ length: count }, (_unused, index) =>
    telemetry({ id: startId + index }),
  );
  return rows.reverse();
}

export function device(overrides: Partial<DeviceDto> = {}): DeviceDto {
  return {
    id: DEVICE_ID,
    firmware_version: "0.1.0",
    status: "online",
    first_seen_at: stamp(0),
    last_seen_at: stamp(120),
    latest: telemetry({ id: 60 }),
    ...overrides,
  };
}

export function anomaly(overrides: Partial<AnomalyDto> = {}): AnomalyDto {
  return {
    id: 1,
    telemetry_id: 1,
    device_id: DEVICE_ID,
    detected_at: stamp(4),
    method: "rule",
    score: 0.91,
    model_version: null,
    reasons: ["overcurrent_rule"],
    voltage_v: 7.84,
    current_a: 3.2,
    power_w: 25.1,
    energy_wh: 0.3,
    ...overrides,
  };
}

export function health(overrides: Partial<HealthDto> = {}): HealthDto {
  return {
    status: "ok",
    database: "ready",
    mqtt: "connected",
    model: "unavailable",
    version: "0.1.0",
    ...overrides,
  };
}
