/**
 * One device's live view: REST seed, socket tail, and the gap between them.
 *
 * REST is the truth and the socket is a live tail, so the order is always the
 * same: seed from REST, open the socket, and then — on the **first** open as
 * well as every reopen — reconcile against REST from the newest row proven
 * contiguous. The first open has a gap of its own: readings can be committed
 * between the snapshot response and the subscription taking effect, and
 * nothing else would ever fetch them.
 *
 * Three rules make the result honest rather than merely plausible:
 *
 *   1. **Catch-up is paginated.** A 300-row page is not proof that 300 rows
 *      were all that was missed; `next_before_id` is followed until the anchor
 *      row comes back or the server says there is no further page.
 *   2. **The anchor only advances on proof.** A catch-up that fails leaves the
 *      anchor where it was, so the retry re-covers the whole gap instead of
 *      starting after it. Live frames are still shown, but they do not move it.
 *   3. **What cannot be proven is said out loud.** A failed catch-up retries on
 *      its own timer, independently of the socket, and the incomplete-history
 *      warning stays up until it succeeds. A gap wider than the retained
 *      window is reported as truncated, never as a full recovery.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiClient } from "../api/client";
import type { ApiClient } from "../api/client";
import type {
  AnomalyEntry,
  DeviceEvent,
  DeviceStatus,
  HistoryQuery,
  TelemetryDto,
} from "../api/contract";
import { ApiError, isAbort } from "../api/errors";
import type { ConnectionState, DeviceSocketOptions } from "./socket";
import { DeviceSocket, backoffDelayMs } from "./socket";
import { MAX_POINTS, attachAnomaly, mergeTelemetry, newestPoint } from "./series";

/** How many rows to seed with, and to ask for per catch-up page. */
export const SEED_LIMIT = 300;

export type SeedPhase = "loading" | "ready" | "failed";

/**
 * What the last catch-up proved.
 *
 * `complete` — every row between the anchor and now was fetched.
 * `truncated` — the gap was wider than the retained window; the newest
 *   `maxPoints` rows are shown and the older missed rows are not.
 * `failed` — REST could not answer; a retry is armed and the warning stands.
 */
export type CatchUpPhase = "idle" | "running" | "complete" | "truncated" | "failed";

export interface DeviceStreamState {
  series: TelemetryDto[];
  anomalies: AnomalyEntry[];
  status: DeviceStatus | null;
  connection: ConnectionState;
  seed: SeedPhase;
  /** The seed failure, if the history could not be loaded at all. */
  error: ApiError | null;
  catchUp: CatchUpPhase;
  /** True while the visible history cannot be proven free of holes. */
  historyIncomplete: boolean;
  /** How many REST pages the last catch-up needed; evidence, not decoration. */
  catchUpPages: number;
  retryNow: () => void;
}

export interface UseDeviceStreamOptions {
  client?: ApiClient;
  limit?: number;
  maxPoints?: number;
  /** Passed through to the socket; tests inject a fake factory and timers. */
  socket?: Partial<Omit<DeviceSocketOptions, "deviceId" | "onEvent" | "onOpen" | "onState">>;
}

const INITIAL_CONNECTION: ConnectionState = {
  phase: "connecting",
  attempt: 0,
  permanentReason: null,
  retryInMs: null,
};

export function useDeviceStream(
  deviceId: string,
  options: UseDeviceStreamOptions = {},
): DeviceStreamState {
  const client = options.client ?? apiClient;
  const limit = options.limit ?? SEED_LIMIT;
  const maxPoints = options.maxPoints ?? MAX_POINTS;

  const [series, setSeries] = useState<TelemetryDto[]>([]);
  const [anomalies, setAnomalies] = useState<AnomalyEntry[]>([]);
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [connection, setConnection] = useState<ConnectionState>(INITIAL_CONNECTION);
  const [seed, setSeed] = useState<SeedPhase>("loading");
  const [error, setError] = useState<ApiError | null>(null);
  const [catchUp, setCatchUp] = useState<CatchUpPhase>("idle");
  const [catchUpPages, setCatchUpPages] = useState(0);
  const [historyIncomplete, setHistoryIncomplete] = useState(false);

  /**
   * Which device session owns the state.
   *
   * Every async completion checks it. Aborting the request is the first line
   * of defence, but a transport that delivers anyway — or a promise already
   * resolved when the device changed — must not be able to write one device's
   * rows into another device's view.
   */
  const generationRef = useRef(0);
  const activeDeviceRef = useRef(deviceId);
  const socketRef = useRef<DeviceSocket | null>(null);
  const manualRetryRef = useRef<(() => void) | null>(null);

  const socketOptions = options.socket;
  // The socket is rebuilt only when the device changes, never on every render.
  const socketConfig = useMemo(() => socketOptions ?? {}, [socketOptions]);

  useEffect(() => {
    activeDeviceRef.current = deviceId;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const current = (): boolean => generationRef.current === generation;
    const abort = new AbortController();

    const setTimer =
      socketConfig.setTimer ?? ((fn: () => void, ms: number) => window.setTimeout(fn, ms));
    const clearTimer = socketConfig.clearTimer ?? ((handle: number) => window.clearTimeout(handle));
    const random = socketConfig.random ?? Math.random;

    // Fresh device: nothing from the previous one may survive.
    setSeries([]);
    setAnomalies([]);
    setStatus(null);
    setSeed("loading");
    setError(null);
    setCatchUp("idle");
    setCatchUpPages(0);
    setHistoryIncomplete(false);
    setConnection(INITIAL_CONNECTION);

    // Session-local, so there is nothing for a later device to inherit.
    let held: TelemetryDto[] = [];
    /** The newest row we can prove nothing is missing before. */
    let anchor: TelemetryDto | null = null;
    /** Whether the newest held row continues the anchor without a hole. */
    let contiguous = false;
    let reconciling = false;
    let reconcileGeneration = 0;
    let queued: TelemetryDto[] = [];
    let retryTimer: number | null = null;
    let retryAttempt = 0;

    const cancelRetry = (): void => {
      if (retryTimer === null) return;
      clearTimer(retryTimer);
      retryTimer = null;
    };

    const absorb = (rows: readonly TelemetryDto[]): void => {
      if (!current() || rows.length === 0) return;
      const merged = mergeTelemetry(held, rows, maxPoints);
      if (merged.added === 0) return;
      held = merged.series;
      setSeries(held);
      // A row only extends the proven region while the series is contiguous;
      // otherwise the anchor stays put and the retry re-covers the gap.
      if (contiguous) anchor = newestPoint(held) ?? anchor;
    };

    /**
     * Fetch until the gap is closed, the window is full, or REST says no.
     *
     * Pages come back newest-first. `from` is the anchor's `received_at` and is
     * inclusive, so the anchor row itself reappears in the last page of the
     * gap — seeing it (or any older id) is the proof that nothing is left
     * between. Without an anchor there is no gap to close: the newest window
     * is all this view ever promises to hold.
     */
    const reconcile = async (supersede = false): Promise<void> => {
      if (!current() || (reconciling && !supersede)) return;
      const attempt = ++reconcileGeneration;
      const currentAttempt = (): boolean => current() && reconcileGeneration === attempt;
      reconciling = true;
      cancelRetry();
      setCatchUp("running");

      const from = anchor;
      const collected: TelemetryDto[] = [];
      let beforeId: number | undefined;
      let pages = 0;
      let proof: "complete" | "truncated" | "failed" | null = null;
      // Enough pages to refill the whole retained window, and one to prove it.
      const maxPages = Math.max(2, Math.ceil(maxPoints / Math.max(1, limit)) + 1);

      try {
        while (pages < maxPages) {
          const query: HistoryQuery = { limit };
          if (from) query.from = from.received_at;
          if (beforeId !== undefined) query.before_id = beforeId;

          const page = await client.fetchTelemetry(deviceId, query, { signal: abort.signal });
          if (!currentAttempt()) return;
          pages += 1;
          collected.push(...page.items);

          const reachedAnchor = from ? page.items.some((row) => row.id <= from.id) : false;
          const next = page.next_before_id ?? null;
          if (reachedAnchor) {
            proof = "complete";
            break;
          }
          if (next === null) {
            proof = from ? "failed" : "complete";
            break;
          }
          if (new Set(collected.map((row) => row.id)).size >= maxPoints) {
            // Without an anchor nothing was missed; a full window is the answer.
            proof = from ? "truncated" : "complete";
            break;
          }
          if (beforeId !== undefined && next >= beforeId) {
            // Pagination that does not move backwards cannot be trusted to
            // terminate, and cannot prove coverage either.
            proof = "failed";
            break;
          }
          beforeId = next;
        }
        // A page cap without an anchor or a full retained window proves
        // nothing; keep the warning and retry instead of claiming recovery.
        proof ??= "failed";
      } catch (failure) {
        if (isAbort(failure) || !currentAttempt()) return;
        proof = "failed";
      }

      if (!currentAttempt()) return;

      // A truncated catch-up still leaves a contiguous *visible* window: the
      // rows it collected outnumber the buffer, so everything older is evicted.
      contiguous = proof !== "failed";
      absorb(collected);

      if (proof === "failed") {
        scheduleRetry();
      } else {
        retryAttempt = 0;
        if (!from && collected.length > 0) {
          // The seed failed earlier and this replaced it; stop reporting an
          // error the view has since recovered from.
          setSeed("ready");
          setError(null);
        }
      }

      setCatchUp(proof);
      setCatchUpPages(pages);
      setHistoryIncomplete(proof !== "complete");
      reconciling = false;

      // Frames held back during catch-up belong after it, in id order.
      const pending = queued;
      queued = [];
      absorb(pending);
    };

    const scheduleRetry = (): void => {
      if (retryTimer !== null || !current()) return;
      retryAttempt += 1;
      retryTimer = setTimer(() => {
        retryTimer = null;
        void reconcile();
      }, backoffDelayMs(retryAttempt, random));
    };

    const handleEvent = (event: DeviceEvent): void => {
      if (!current()) return;
      if (event.type === "telemetry") {
        if (reconciling) {
          // Merging a live frame mid-catch-up would move the newest row past
          // rows still being fetched. It waits; it is not dropped.
          queued.push(event.data);
          if (queued.length > maxPoints) queued = queued.slice(-maxPoints);
          return;
        }
        absorb([event.data]);
        return;
      }
      if (event.type === "anomaly") {
        const anomaly = event.data;
        setAnomalies((existing) =>
          existing.some((item) => item.id === anomaly.id) ? existing : [anomaly, ...existing],
        );
        const marked = attachAnomaly(held, anomaly.telemetry_id, {
          id: anomaly.id,
          method: anomaly.method,
          score: anomaly.score,
          model_version: anomaly.model_version,
          reasons: anomaly.reasons,
        });
        if (marked !== held) {
          held = marked;
          setSeries(held);
        }
        return;
      }
      setStatus(event.data.status);
    };

    const socket = new DeviceSocket({
      ...socketConfig,
      deviceId,
      onEvent: handleEvent,
      // Every open, the first included: both have a gap behind them.
      onOpen: () => {
        // A new subscription invalidates any older REST snapshot still in
        // flight. Only this opening may declare the gap fully recovered.
        void reconcile(true);
      },
      onState: (next) => {
        if (current()) setConnection(next);
      },
    });
    socketRef.current = socket;

    manualRetryRef.current = () => {
      cancelRetry();
      socket.retryNow();
      // A socket that is already healthy will not reopen, so the catch-up that
      // failed under it needs its own nudge.
      void reconcile();
    };

    const seedSeries = async (): Promise<void> => {
      try {
        const page = await client.fetchTelemetry(deviceId, { limit }, { signal: abort.signal });
        if (!current()) return;
        contiguous = true;
        // The backend returns newest first; a series reads oldest first.
        absorb([...page.items].reverse());
        anchor = newestPoint(held);
        setSeed("ready");
      } catch (failure) {
        if (isAbort(failure) || !current()) return;
        setError(
          failure instanceof ApiError
            ? failure
            : new ApiError("protocol", "history could not be loaded"),
        );
        setSeed("failed");
      }
      if (!current()) return;
      // The socket opens whether or not the seed worked: live values are still
      // worth showing, and the first catch-up will fetch what the seed missed.
      socket.start();
    };

    void seedSeries();

    return () => {
      // Invalidate first: a response already in flight must find itself stale
      // even if the transport delivers it regardless of the abort.
      generationRef.current = generation + 1;
      abort.abort();
      cancelRetry();
      socket.stop();
      socketRef.current = null;
      manualRetryRef.current = null;
    };
  }, [deviceId, client, limit, maxPoints, socketConfig]);

  const retryNow = useCallback(() => {
    manualRetryRef.current?.();
  }, []);

  if (activeDeviceRef.current !== deviceId) {
    return {
      series: [],
      anomalies: [],
      status: null,
      connection: INITIAL_CONNECTION,
      seed: "loading",
      error: null,
      catchUp: "idle",
      historyIncomplete: false,
      catchUpPages: 0,
      retryNow,
    };
  }

  return {
    series,
    anomalies,
    status,
    connection,
    seed,
    error,
    catchUp,
    historyIncomplete,
    catchUpPages,
    retryNow,
  };
}
