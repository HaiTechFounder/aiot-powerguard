/**
 * What "Live" is allowed to mean, decided in one place.
 *
 * A green Live badge is a claim about the device, not about this browser's
 * socket. Three independent things can each break it and they are *not* the
 * same fact:
 *
 *   1. the WebSocket transport between this page and the backend,
 *   2. the backend's own reachability and its link to the MQTT broker,
 *   3. the device's status and whether its newest reading is recent.
 *
 * An open socket to a healthy backend proves only that the backend would tell
 * us if something arrived. With the broker down, or the device offline, or its
 * last reading a day old, nothing can arrive — and saying "Live" over a
 * day-old measurement is the dashboard asserting something it cannot know.
 * So every one of the five conditions has to hold, and when one does not, the
 * badge names the first one that failed rather than degrading to a vague
 * warning.
 *
 * Nothing here fabricates a reading or a status: each branch reports a state
 * the API actually published, or the absence of one.
 */

import type { DeviceStatus, HealthDto, TelemetryDto } from "../api/contract";
import type { ConnectionState } from "./socket";

/**
 * How old the newest reading may be before it stops counting as live.
 *
 * This mirrors the backend's `stale_after_s`
 * (`backend/src/powerguard/config.py`, default 15s), which is what marks a
 * device `stale` server-side. The value is not published by `/health` or by
 * any v1 contract, so the browser cannot read the real one; `VITE_STALE_AFTER_S`
 * exists so a deployment that changes the backend default can keep the two in
 * step without editing code. If the backend ever serves the threshold, this
 * constant should be replaced by that value rather than joined by it.
 */
function configuredStaleAfterMs(): number {
  const raw = import.meta.env?.VITE_STALE_AFTER_S as string | undefined;
  const parsed = raw === undefined || raw === "" ? Number.NaN : Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed * 1000 : 15_000;
}

export const STALE_AFTER_MS = configuredStaleAfterMs();

export type LiveLevel =
  | "live"
  | "stale"
  | "device-offline"
  | "device-stale"
  | "broker-down"
  | "transport-down"
  | "transport-connecting"
  | "backend-down"
  | "no-readings"
  | "unknown";

export type LiveTone = "ok" | "warn" | "error" | "muted";

export interface LiveState {
  level: LiveLevel;
  /** The badge's own words; short enough to sit next to the device name. */
  label: string;
  tone: LiveTone;
  /** One sentence of why, when there is more to say than the label. */
  detail: string | null;
  /** True only when all five conditions hold. */
  isLive: boolean;
}

export interface LiveStateInput {
  deviceId: string;
  /** Null while `/health` has not answered yet. */
  health: HealthDto | null;
  /** Set when `/health` failed; the backend is not reachable. */
  healthFailed: boolean;
  connection: ConnectionState;
  /** The live `status` frame if one arrived, else the registry's value. */
  deviceStatus: DeviceStatus | null;
  /** The newest reading this view holds, whatever its age. */
  latest: TelemetryDto | null;
  /** Milliseconds since the epoch; passed in so the decision stays pure. */
  now: number;
  staleAfterMs?: number;
}

/** Milliseconds since a reading was received, or null if it cannot be read. */
export function ageMs(latest: TelemetryDto | null, now: number): number | null {
  if (!latest) return null;
  const at = new Date(latest.received_at).getTime();
  return Number.isNaN(at) ? null : now - at;
}

export function evaluateLiveState(input: LiveStateInput): LiveState {
  const {
    deviceId,
    health,
    healthFailed,
    connection,
    deviceStatus,
    latest,
    now,
    staleAfterMs = STALE_AFTER_MS,
  } = input;

  // 1. The backend. Without it nothing below can be established at all.
  if (healthFailed) {
    return {
      level: "backend-down",
      label: "Backend unavailable",
      tone: "error",
      detail: "The backend could not be reached. Readings already loaded are still shown.",
      isLive: false,
    };
  }
  if (!health) {
    return {
      level: "unknown",
      label: "Checking…",
      tone: "muted",
      detail: null,
      isLive: false,
    };
  }
  // The backend normally answers a degraded database with 503, which lands in
  // `healthFailed`. A body that nonetheless says so is believed, not skipped.
  if (health.status !== "ok" || health.database !== "ready") {
    return {
      level: "backend-down",
      label: "Backend degraded",
      tone: "error",
      detail: "The backend reports its database is not ready. Readings already loaded are still shown.",
      isLive: false,
    };
  }

  // 2. A socket that stopped for good cannot recover on its own.
  if (connection.permanentReason) {
    return {
      level: "transport-down",
      label: "Live updates stopped",
      tone: "error",
      detail: `${connection.permanentReason}. Readings already loaded are still shown.`,
      isLive: false,
    };
  }

  // 3. The broker. With it down the backend has no source of new telemetry,
  //    so an open socket proves nothing about the device.
  if (health.mqtt !== "connected") {
    return {
      level: "broker-down",
      label: "Broker disconnected",
      tone: "warn",
      detail:
        "The backend is not connected to the MQTT broker, so no new reading can arrive. History stays readable.",
      isLive: false,
    };
  }

  // 4. This page's own transport.
  if (connection.phase === "reconnecting") {
    return {
      level: "transport-down",
      label: "Reconnecting",
      tone: "warn",
      detail: `Live connection lost (attempt ${connection.attempt}). Readings already loaded are kept.`,
      isLive: false,
    };
  }
  if (connection.phase !== "open") {
    return {
      level: "transport-connecting",
      label: "Connecting…",
      tone: "muted",
      detail: "Opening the live connection.",
      isLive: false,
    };
  }

  // 5. The device itself, as the backend last reported it.
  if (deviceStatus === "offline") {
    return {
      level: "device-offline",
      label: "Device offline",
      tone: "warn",
      detail: "The device is not reporting. The charts below are its recorded history.",
      isLive: false,
    };
  }
  if (deviceStatus === "stale") {
    return {
      level: "device-stale",
      label: "Device stale",
      tone: "warn",
      detail: "The device stopped reporting without disconnecting. History stays readable.",
      isLive: false,
    };
  }
  if (deviceStatus === null) {
    return {
      level: "unknown",
      label: "Status unknown",
      tone: "muted",
      detail: "No device status has been read yet.",
      isLive: false,
    };
  }

  // 6. A reading, belonging to this device, recent enough to still be true.
  if (!latest || latest.device_id !== deviceId) {
    return {
      level: "no-readings",
      label: "No live readings",
      tone: "muted",
      detail: "No reading for this device has arrived on the live connection yet.",
      isLive: false,
    };
  }
  const age = ageMs(latest, now);
  if (age === null) {
    return {
      level: "no-readings",
      label: "No live readings",
      tone: "muted",
      detail: "The newest reading has no readable timestamp.",
      isLive: false,
    };
  }
  // `received_at` is the server's clock and `now` is this browser's. A reading
  // stamped well in the future means the two disagree, and then its age is
  // unknowable -- a day-old reading from a server ahead of us would otherwise
  // look brand new. Small skew (within the threshold) is tolerated.
  if (age < -staleAfterMs) {
    return {
      level: "unknown",
      label: "Clock mismatch",
      tone: "warn",
      detail:
        "The newest reading is stamped ahead of this computer's clock, so its age cannot be judged.",
      isLive: false,
    };
  }
  if (age > staleAfterMs) {
    return {
      level: "stale",
      label: "Stale data",
      tone: "warn",
      detail: `No reading for ${Math.round(age / 1000)}s; the threshold is ${Math.round(
        staleAfterMs / 1000,
      )}s.`,
      isLive: false,
    };
  }

  return { level: "live", label: "Live", tone: "ok", detail: null, isLive: true };
}
