import { apiClient } from "../api/client";
import type { HealthDto } from "../api/contract";
import type { ApiResource } from "./useApiResource";
import { useApiResource } from "./useApiResource";

/** Ten seconds: often enough to notice an outage, rare enough to be quiet. */
export const HEALTH_POLL_MS = 10_000;

export function useHealth(client = apiClient, pollMs = HEALTH_POLL_MS): ApiResource<HealthDto> {
  return useApiResource<HealthDto>(
    (signal) => client.fetchHealth({ signal }),
    [client],
    { pollMs },
  );
}

/**
 * Whether the anomaly detector is actually running.
 *
 * Phase 03 ships `UnavailableInference`, which never flags, so this is false
 * today. A view that treats false as "no anomalies found" is reporting a clean
 * bill of health from a detector that is switched off.
 */
export function isModelReady(health: HealthDto | null): boolean {
  return health?.model === "ready";
}
