/**
 * The live verdict, re-decided on a timer as well as on new data.
 *
 * Freshness is the one input that changes with nothing happening: a reading
 * that was current a moment ago goes stale purely because time passed. If the
 * badge only recomputed when a frame arrived, the last frame before an outage
 * would leave "Live" on screen forever — which is exactly the failure this
 * hook exists to prevent. So it ticks, and the interval is cleared on unmount.
 */

import { useEffect, useMemo, useState } from "react";

import type { LiveState, LiveStateInput } from "../realtime/liveState";
import { evaluateLiveState } from "../realtime/liveState";

/** A second: fine enough against a 15s threshold, cheap enough to ignore. */
export const LIVE_TICK_MS = 1_000;

export function useLiveState(
  input: Omit<LiveStateInput, "now">,
  tickMs = LIVE_TICK_MS,
): LiveState {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), tickMs);
    return () => window.clearInterval(timer);
  }, [tickMs]);

  const {
    deviceId,
    health,
    healthFailed,
    connection,
    deviceStatus,
    latest,
    staleAfterMs,
  } = input;

  return useMemo(
    () =>
      evaluateLiveState({
        deviceId,
        health,
        healthFailed,
        connection,
        deviceStatus,
        latest,
        staleAfterMs,
        now,
      }),
    [deviceId, health, healthFailed, connection, deviceStatus, latest, staleAfterMs, now],
  );
}
