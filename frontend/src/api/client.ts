/**
 * The typed REST client for PowerGuard v1.
 *
 * One function per published endpoint, and nothing else. Every failure becomes
 * an `ApiError` carrying the backend's own envelope where there is one, so a
 * view can tell "the backend said no" from "nothing answered" — they need
 * different words on screen.
 */

import type {
  AnomalyPageDto,
  DeviceListDto,
  ErrorEnvelope,
  HealthDto,
  HistoryQuery,
  TelemetryDto,
  TelemetryPageDto,
} from "./contract";
import { LIMIT_MAX, LIMIT_MIN } from "./contract";
import { ApiError } from "./errors";

/** Blank by default: the Vite dev proxy serves `/api` from the same origin. */
const API_BASE = (import.meta.env?.VITE_API_BASE_URL as string | undefined) ?? "";

/**
 * Absolute request URL.
 *
 * A browser resolves a relative path against the document, but Node's `fetch`
 * refuses one outright — so the origin is made explicit here rather than left
 * to whichever runtime the code happens to be in.
 */
export function resolveUrl(path: string): string {
  if (API_BASE) return `${API_BASE}${path}`;
  const origin = typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:8000";
  return new URL(path, origin).toString();
}

export interface RequestOptions {
  signal?: AbortSignal;
}

export function buildHistoryQuery(query: HistoryQuery = {}): string {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  if (query.limit !== undefined) {
    if (!Number.isInteger(query.limit) || query.limit < LIMIT_MIN || query.limit > LIMIT_MAX) {
      throw new ApiError("protocol", `limit must be ${LIMIT_MIN}..${LIMIT_MAX}`);
    }
    params.set("limit", String(query.limit));
  }
  if (query.before_id !== undefined) {
    if (!Number.isInteger(query.before_id) || query.before_id < 1) {
      throw new ApiError("protocol", "before_id must be a positive row id");
    }
    params.set("before_id", String(query.before_id));
  }
  const rendered = params.toString();
  return rendered ? `?${rendered}` : "";
}

function isEnvelope(body: unknown): body is ErrorEnvelope {
  if (typeof body !== "object" || body === null) return false;
  const error = (body as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(resolveUrl(path), {
      headers: { Accept: "application/json" },
      signal: options.signal,
    });
  } catch (error) {
    // An abort is the caller's own doing; it must not be reported as an outage.
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("network", "the backend could not be reached", {
      details: error instanceof Error ? error.message : String(error),
    });
  }

  const body = await readBody(response);

  if (!response.ok) {
    if (isEnvelope(body)) {
      throw new ApiError("api", body.error.message, {
        status: response.status,
        code: body.error.code,
        details: body.error.details,
      });
    }
    // A failure that is not in the envelope shape is still a failure.
    throw new ApiError("protocol", `request failed with status ${response.status}`, {
      status: response.status,
    });
  }

  if (body === null) {
    throw new ApiError("protocol", "the backend returned an empty body", {
      status: response.status,
    });
  }
  return body as T;
}

export async function fetchHealth(options?: RequestOptions): Promise<HealthDto> {
  return request<HealthDto>("/api/v1/health", options);
}

export async function fetchDevices(options?: RequestOptions): Promise<DeviceListDto> {
  return request<DeviceListDto>("/api/v1/devices", options);
}

export async function fetchLatest(
  deviceId: string,
  options?: RequestOptions,
): Promise<TelemetryDto> {
  return request<TelemetryDto>(
    `/api/v1/devices/${encodeURIComponent(deviceId)}/latest`,
    options,
  );
}

export async function fetchTelemetry(
  deviceId: string,
  query: HistoryQuery = {},
  options?: RequestOptions,
): Promise<TelemetryPageDto> {
  const path = `/api/v1/devices/${encodeURIComponent(deviceId)}/telemetry`;
  return request<TelemetryPageDto>(`${path}${buildHistoryQuery(query)}`, options);
}

export async function fetchAnomalies(
  deviceId: string,
  query: HistoryQuery = {},
  options?: RequestOptions,
): Promise<AnomalyPageDto> {
  const path = `/api/v1/devices/${encodeURIComponent(deviceId)}/anomalies`;
  return request<AnomalyPageDto>(`${path}${buildHistoryQuery(query)}`, options);
}

export const apiClient = {
  fetchHealth,
  fetchDevices,
  fetchLatest,
  fetchTelemetry,
  fetchAnomalies,
};

export type ApiClient = typeof apiClient;
