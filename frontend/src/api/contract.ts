/**
 * The Phase 03 v1 contracts, mirrored for the browser.
 *
 * Every shape here is published by the backend's OpenAPI document and by
 * `API_CONTRACT.md`. Nothing is invented: a field the backend does not send
 * must not appear, and a field the backend marks required must not be optional
 * here. `tests/contract` checks both directions against a committed fixture of
 * the real `openapi.json`, so the two cannot drift apart silently.
 */

/** RFC 3339 UTC with milliseconds and a literal `Z`, exactly as sent. */
export type Rfc3339 = string;

export type DeviceStatus = "online" | "offline" | "stale";

export interface AnomalySummary {
  id: number;
  method: string;
  score: number | null;
  model_version: string | null;
  reasons: string[];
}

export interface TelemetryDto {
  id: number;
  device_id: string;
  boot_id: string;
  seq: number;
  /** Diagnostic only, and null until the firmware has a time source. */
  sampled_at: Rfc3339 | null;
  /** ADR-006: server receive time, and the authoritative order. */
  received_at: Rfc3339;
  voltage_v: number;
  current_a: number;
  power_w: number;
  energy_wh: number;
  sensor_status: string;
  anomaly?: AnomalySummary | null;
}

export interface DeviceDto {
  id: string;
  firmware_version: string;
  status: DeviceStatus;
  first_seen_at: Rfc3339;
  last_seen_at: Rfc3339;
  latest?: TelemetryDto | null;
}

export interface DeviceListDto {
  items: DeviceDto[];
}

/**
 * The anomaly fields every transport carries.
 *
 * REST adds the measurements the verdict was based on; the WebSocket frame
 * does not send them (`realtime/events.py: anomaly_event`). Keeping the two
 * apart is the point — typing a socket frame as if it carried measurements
 * would render `undefined` as a reading.
 */
export interface AnomalyRecord {
  id: number;
  telemetry_id: number;
  device_id: string;
  detected_at: Rfc3339;
  method: string;
  score: number | null;
  model_version: string | null;
  reasons: string[];
}

/** The measurements REST attaches to a stored verdict. */
export interface AnomalyMeasurements {
  voltage_v: number;
  current_a: number;
  power_w: number;
  energy_wh: number;
}

/** `GET /anomalies`: the verdict plus the reading it was based on. */
export interface AnomalyDto extends AnomalyRecord, AnomalyMeasurements {}

/** A `anomaly` frame's payload: the verdict alone. */
export type AnomalyEventData = AnomalyRecord;

/**
 * Anything the anomaly areas may be asked to render.
 *
 * A REST row has measurements, a socket frame does not, and the difference is
 * shown as an em dash rather than papered over.
 */
export type AnomalyEntry = AnomalyRecord & Partial<AnomalyMeasurements>;

export interface TelemetryPageDto {
  items: TelemetryDto[];
  /** Set only when the backend proved another page exists. */
  next_before_id?: number | null;
}

export interface AnomalyPageDto {
  items: AnomalyDto[];
  next_before_id?: number | null;
}

export interface HealthDto {
  status: string;
  database: string;
  /** "connected" | "disconnected". A broker outage is not an unhealthy service. */
  mqtt: string;
  /** "ready" | "unavailable". Never read this as "no anomalies". */
  model: string;
  version: string;
}

/** The single error envelope every failing endpoint returns. */
export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

// -- WebSocket v1 ----------------------------------------------------------

export type EventType = "telemetry" | "anomaly" | "status";

export interface StatusEventData {
  device_id: string;
  status: DeviceStatus;
  last_seen_at: Rfc3339;
}

interface Envelope<T extends EventType, D> {
  schema_version: 1;
  type: T;
  emitted_at: Rfc3339;
  data: D;
}

export type TelemetryEvent = Envelope<"telemetry", TelemetryDto>;
export type AnomalyEvent = Envelope<"anomaly", AnomalyEventData>;
export type StatusEvent = Envelope<"status", StatusEventData>;
export type DeviceEvent = TelemetryEvent | AnomalyEvent | StatusEvent;

/** History query parameters. `from` is inclusive, `to` exclusive and later. */
export interface HistoryQuery {
  from?: Rfc3339;
  to?: Rfc3339;
  /** 1..5000; the backend defaults to 500. */
  limit?: number;
  /** Positive row id, for backward pagination. */
  before_id?: number;
}

export const LIMIT_MIN = 1;
export const LIMIT_MAX = 5000;
