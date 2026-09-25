/**
 * What "Live" may claim, and when it must stop claiming it.
 *
 * Every case here is one of the ways an open WebSocket can sit in front of a
 * device that is telling us nothing. The badge is only allowed to be green
 * when the backend, the broker, the socket, the device and the newest reading
 * all agree — so each test knocks out exactly one of the five and expects the
 * verdict to fall.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionState } from "../../src/realtime/socket";
import { STALE_AFTER_MS, evaluateLiveState } from "../../src/realtime/liveState";
import { useLiveState } from "../../src/hooks/useLiveState";
import { DEVICE_ID, health, telemetry } from "../builders";

const NOW = Date.UTC(2026, 5, 1, 12, 0, 0);

const OPEN: ConnectionState = {
  phase: "open",
  attempt: 0,
  permanentReason: null,
  retryInMs: null,
};

/** A reading `ageMs` old, belonging to the device under test. */
function reading(ageMs: number) {
  return telemetry({ received_at: new Date(NOW - ageMs).toISOString() });
}

function evaluate(overrides: Partial<Parameters<typeof evaluateLiveState>[0]> = {}) {
  return evaluateLiveState({
    deviceId: DEVICE_ID,
    health: health({ mqtt: "connected" }),
    healthFailed: false,
    connection: OPEN,
    deviceStatus: "online",
    latest: reading(1_000),
    now: NOW,
    ...overrides,
  });
}

describe("the live verdict", () => {
  it("is Live only when backend, broker, socket, device and freshness all hold", () => {
    const state = evaluate();
    expect(state.isLive).toBe(true);
    expect(state.label).toBe("Live");
    expect(state.tone).toBe("ok");
  });

  it("is not Live while the broker is disconnected, however healthy the socket", () => {
    const state = evaluate({ health: health({ mqtt: "disconnected" }) });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("Broker disconnected");
  });

  it("is not Live while the device is offline, however healthy the socket", () => {
    const state = evaluate({ deviceStatus: "offline" });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("Device offline");
  });

  it("distinguishes a stale device from an offline one", () => {
    expect(evaluate({ deviceStatus: "stale" }).label).toBe("Device stale");
  });

  it("calls a reading older than the threshold stale, even with the device online", () => {
    const state = evaluate({ latest: reading(STALE_AFTER_MS + 1_000) });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("Stale data");
    expect(state.detail).toMatch(/threshold is 15s/);
  });

  it("keeps a reading exactly at the threshold live", () => {
    expect(evaluate({ latest: reading(STALE_AFTER_MS) }).isLive).toBe(true);
  });

  it("is not Live when the backend body reports a degraded database", () => {
    const state = evaluate({
      health: { ...health({ mqtt: "connected" }), status: "degraded", database: "unavailable" },
    });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("Backend degraded");
  });

  it("tolerates small clock skew but never calls a far-future reading live", () => {
    expect(evaluate({ latest: reading(-2_000) }).isLive).toBe(true);
    const state = evaluate({ latest: reading(-(STALE_AFTER_MS + 60_000)) });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("Clock mismatch");
  });

  it("reports an unreachable backend before anything else", () => {
    const state = evaluate({ healthFailed: true, health: null });
    expect(state.label).toBe("Backend unavailable");
    expect(state.tone).toBe("error");
  });

  it("reports a reconnecting socket rather than a device verdict", () => {
    const state = evaluate({
      connection: { phase: "reconnecting", attempt: 2, permanentReason: null, retryInMs: 4_000 },
    });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("Reconnecting");
    expect(state.detail).toMatch(/attempt 2/);
  });

  it("reports a permanently closed socket as stopped, not as reconnecting", () => {
    const state = evaluate({
      connection: {
        phase: "closed",
        attempt: 0,
        permanentReason: "the backend does not know this device",
        retryInMs: null,
      },
    });
    expect(state.label).toBe("Live updates stopped");
  });

  // A frame for another device proves nothing about this one; it must never
  // be counted as this device's freshness.
  it("ignores a reading that belongs to a different device", () => {
    const state = evaluate({ latest: telemetry({ device_id: "someone-else" }) });
    expect(state.isLive).toBe(false);
    expect(state.label).toBe("No live readings");
  });

  it("says nothing is known before health has answered", () => {
    expect(evaluate({ health: null }).label).toBe("Checking…");
  });
});

describe("freshness over time", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  // The whole point of the timer: the last frame before an outage must not
  // leave a green badge on screen for ever.
  it("expires without any new reading arriving", () => {
    vi.useFakeTimers();
    const now = Date.now();
    const input = {
      deviceId: DEVICE_ID,
      health: health({ mqtt: "connected" }),
      healthFailed: false,
      connection: OPEN,
      deviceStatus: "online" as const,
      latest: telemetry({ received_at: new Date(now).toISOString() }),
    };

    const { result, unmount } = renderHook(() => useLiveState(input));
    expect(result.current.isLive).toBe(true);

    act(() => {
      vi.advanceTimersByTime(STALE_AFTER_MS + 2_000);
    });

    expect(result.current.isLive).toBe(false);
    expect(result.current.label).toBe("Stale data");

    // The interval is the hook's own; it must not outlive the component.
    const pending = vi.getTimerCount();
    unmount();
    expect(vi.getTimerCount()).toBeLessThan(pending);
  });
});
