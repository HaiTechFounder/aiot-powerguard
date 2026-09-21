import { apiClient } from "../api/client";
import type { DeviceListDto } from "../api/contract";
import type { ApiResource } from "./useApiResource";
import { useApiResource } from "./useApiResource";

export const DEVICES_POLL_MS = 15_000;

export function useDevices(
  client = apiClient,
  pollMs = DEVICES_POLL_MS,
): ApiResource<DeviceListDto> {
  return useApiResource<DeviceListDto>(
    (signal) => client.fetchDevices({ signal }),
    [client],
    { pollMs },
  );
}
