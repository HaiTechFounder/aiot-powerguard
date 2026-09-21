import { apiClient } from "../api/client";
import type { AnomalyPageDto } from "../api/contract";
import type { ApiResource } from "./useApiResource";
import { useApiResource } from "./useApiResource";

export const ANOMALY_PAGE_SIZE = 100;

export function useAnomalies(
  deviceId: string,
  client = apiClient,
  limit = ANOMALY_PAGE_SIZE,
): ApiResource<AnomalyPageDto> {
  return useApiResource<AnomalyPageDto>(
    (signal) => client.fetchAnomalies(deviceId, { limit }, { signal }),
    [deviceId, client, limit],
  );
}
