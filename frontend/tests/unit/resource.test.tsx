import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../src/api/client";
import type { AnomalyPageDto } from "../../src/api/contract";
import { useAnomalies } from "../../src/hooks/useAnomalies";
import { anomaly } from "../builders";

describe("device-scoped REST state", () => {
  it("never shows device A history on B, even when A resolves late", async () => {
    const pending = new Map<string, (page: AnomalyPageDto) => void>();
    const client = {
      fetchAnomalies: vi.fn((deviceId: string) =>
        new Promise<AnomalyPageDto>((resolve) => pending.set(deviceId, resolve)),
      ),
    } as unknown as ApiClient;
    const { result, rerender, unmount } = renderHook(
      ({ deviceId }) => useAnomalies(deviceId, client),
      { initialProps: { deviceId: "device-A" } },
    );
    await waitFor(() => expect(pending.has("device-A")).toBe(true));

    rerender({ deviceId: "device-B" });
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(pending.has("device-B")).toBe(true));

    act(() => pending.get("device-B")?.({ items: [anomaly({ id: 2, device_id: "device-B" })] }));
    await waitFor(() => expect(result.current.data?.items[0]?.device_id).toBe("device-B"));

    act(() => pending.get("device-A")?.({ items: [anomaly({ id: 1, device_id: "device-A" })] }));
    expect(result.current.data?.items[0]?.device_id).toBe("device-B");
    unmount();
  });

  it("ignores a late response after unmount", async () => {
    let resolve: ((page: AnomalyPageDto) => void) | undefined;
    const client = {
      fetchAnomalies: vi.fn(() =>
        new Promise<AnomalyPageDto>((complete) => {
          resolve = complete;
        }),
      ),
    } as unknown as ApiClient;
    const { result, unmount } = renderHook(() => useAnomalies("device-A", client));
    await waitFor(() => expect(resolve).toBeDefined());
    unmount();

    await act(async () => {
      resolve?.({ items: [anomaly({ device_id: "device-A" })] });
    });
    expect(result.current.data).toBeNull();
  });
});
