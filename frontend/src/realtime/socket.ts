/**
 * The device WebSocket, and the policy for losing it.
 *
 * Two close codes are verdicts, not accidents: 4400 says the device id is
 * malformed and 4404 says the device does not exist. Retrying either would be
 * a loop that can never succeed, so they stop the socket for good. Everything
 * else — 1013 when the backend drops a slow client, an ordinary close, a
 * network failure — is retried under a capped exponential backoff with a fresh
 * jitter per attempt, because several dashboards reopening in lockstep is the
 * problem jitter exists to prevent.
 *
 * At most one socket and one timer exist at a time, including when the
 * operator presses "retry now" while an attempt is already in flight: the
 * previous socket is retired — handlers detached, then closed — before the
 * replacement is created. A socket carries the generation it was opened under,
 * so a callback from a retired connection is recognised and ignored rather
 * than mistaken for the live one.
 */

import type { DeviceEvent } from "../api/contract";

export const CLOSE_MALFORMED_DEVICE_ID = 4400;
export const CLOSE_UNKNOWN_DEVICE = 4404;

export const BACKOFF_BASE_MS = 1_000;
export const BACKOFF_CAP_MS = 30_000;
const MAX_ATTEMPT_SHIFT = 5; // 1s, 2s, 4s, 8s, 16s, then the cap

/** Close codes that mean "this will never work"; everything else may retry. */
export function isPermanentClose(code: number): boolean {
  return code === CLOSE_MALFORMED_DEVICE_ID || code === CLOSE_UNKNOWN_DEVICE;
}

export function describeClose(code: number): string {
  if (code === CLOSE_UNKNOWN_DEVICE) return "the backend does not know this device";
  if (code === CLOSE_MALFORMED_DEVICE_ID) return "that device id is not valid";
  if (code === 1013) return "the backend dropped this connection to protect itself";
  return "the live connection was lost";
}

/**
 * Delay before attempt `attempt` (1-based), in milliseconds.
 *
 * Full jitter across the upper half of each window: never below half the base,
 * never above it, and never above the cap.
 */
export function backoffDelayMs(attempt: number, random: () => number = Math.random): number {
  const shift = Math.min(Math.max(attempt, 1), MAX_ATTEMPT_SHIFT + 1) - 1;
  const base = Math.min(BACKOFF_BASE_MS * 2 ** shift, BACKOFF_CAP_MS);
  return Math.round(base / 2 + random() * (base / 2));
}

export type ConnectionPhase = "connecting" | "open" | "reconnecting" | "closed";

export interface ConnectionState {
  phase: ConnectionPhase;
  /** How many reopen attempts have been made since the last healthy socket. */
  attempt: number;
  /** Set when the socket stopped for good; the caller shows it and gives up. */
  permanentReason: string | null;
  /** Milliseconds until the next attempt, when one is scheduled. */
  retryInMs: number | null;
}

export interface DeviceSocketOptions {
  deviceId: string;
  onEvent: (event: DeviceEvent) => void;
  /**
   * Called on every open, first one included, so the caller can reconcile
   * against REST. `reopened` says whether this socket replaced an earlier
   * healthy one; a first open has its own gap — between the REST snapshot and
   * the subscription — so the caller reconciles either way.
   */
  onOpen: (info: { reopened: boolean }) => void;
  onState: (state: ConnectionState) => void;
  baseUrl?: string;
  random?: () => number;
  /** Injected in tests; the browser's own in production. */
  socketFactory?: (url: string) => WebSocket;
  setTimer?: (fn: () => void, ms: number) => number;
  clearTimer?: (handle: number) => void;
}

function defaultBaseUrl(): string {
  const configured = import.meta.env?.VITE_WS_BASE_URL as string | undefined;
  if (configured) return configured;
  if (typeof window === "undefined") return "ws://127.0.0.1:8000";
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}`;
}

export class DeviceSocket {
  private readonly options: DeviceSocketOptions;
  private socket: WebSocket | null = null;
  private timer: number | null = null;
  private attempt = 0;
  private stopped = false;
  private everOpened = false;
  private connected = false;
  private manualAttemptInFlight = false;
  /** Incremented whenever a socket is retired; stale callbacks carry an old one. */
  private generation = 0;

  constructor(options: DeviceSocketOptions) {
    this.options = options;
  }

  private get setTimer() {
    return this.options.setTimer ?? ((fn: () => void, ms: number) => window.setTimeout(fn, ms));
  }

  private get clearTimer() {
    return this.options.clearTimer ?? ((handle: number) => window.clearTimeout(handle));
  }

  private report(state: ConnectionState): void {
    if (this.stopped && state.phase !== "closed") return;
    this.options.onState(state);
  }

  start(): void {
    this.stopped = false;
    this.manualAttemptInFlight = false;
    this.open("connecting");
  }

  /**
   * Detach and close the current socket, if there is one.
   *
   * Handlers are cleared *before* `close()`, so the retired connection cannot
   * report its own closure and schedule a retry that the replacement already
   * owns. This is what makes two live sockets structurally impossible.
   */
  private retire(): void {
    const socket = this.socket;
    this.socket = null;
    this.connected = false;
    this.generation += 1;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    socket.close();
  }

  private open(phase: ConnectionPhase): void {
    if (this.stopped) return;
    // Whatever was there loses ownership first: one stream, one socket.
    this.retire();
    const generation = this.generation;
    this.report({ phase, attempt: this.attempt, permanentReason: null, retryInMs: null });

    const base = this.options.baseUrl ?? defaultBaseUrl();
    const url = `${base}/ws/v1/devices/${encodeURIComponent(this.options.deviceId)}`;
    const factory = this.options.socketFactory ?? ((target: string) => new WebSocket(target));
    const socket = factory(url);
    this.socket = socket;

    const owns = (): boolean =>
      this.socket === socket && this.generation === generation && !this.stopped;

    socket.onopen = () => {
      // A frame from a replaced socket must never be mistaken for a live one.
      if (!owns()) return;
      const reopened = this.everOpened;
      this.everOpened = true;
      this.connected = true;
      this.manualAttemptInFlight = false;
      this.attempt = 0; // a healthy socket resets the backoff
      this.report({ phase: "open", attempt: 0, permanentReason: null, retryInMs: null });
      this.options.onOpen({ reopened });
    };

    socket.onmessage = (message: MessageEvent<string>) => {
      if (!owns()) return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(message.data) as unknown;
      } catch {
        return; // the contract is JSON only; anything else is not ours
      }
      if (isDeviceEvent(parsed)) this.options.onEvent(parsed);
    };

    socket.onclose = (event: CloseEvent) => {
      // A stale socket closing changes nothing: it no longer owns the stream.
      if (this.socket !== socket || this.generation !== generation) return;
      this.socket = null;
      this.connected = false;
      this.manualAttemptInFlight = false;
      if (this.stopped) return;

      if (isPermanentClose(event.code)) {
        this.stopped = true;
        this.report({
          phase: "closed",
          attempt: this.attempt,
          permanentReason: describeClose(event.code),
          retryInMs: null,
        });
        return;
      }
      this.scheduleRetry();
    };

    socket.onerror = () => {
      // `onclose` always follows, and it carries the code worth acting on.
    };
  }

  private scheduleRetry(): void {
    if (this.stopped || this.timer !== null) return;
    this.attempt += 1;
    const delay = backoffDelayMs(this.attempt, this.options.random);
    this.report({
      phase: "reconnecting",
      attempt: this.attempt,
      permanentReason: null,
      retryInMs: delay,
    });
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.open("reconnecting");
    }, delay);
  }

  /**
   * Connect now instead of waiting out the current delay.
   *
   * During pending backoff or an initial connecting attempt, replace once.
   * Repeated clicks while that replacement is in flight do nothing, and a
   * healthy connection is never torn down for a manual retry.
   */
  retryNow(): void {
    if (this.stopped) return;
    // A healthy socket needs no replacement. One manual attempt is enough
    // until it opens or fails; repeated clicks must not storm the backend.
    if (this.connected || this.manualAttemptInFlight) return;
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    this.manualAttemptInFlight = true;
    // `open()` retires the predecessor itself; this is the manual path into it.
    this.open("reconnecting");
  }

  /** Close for good: no further frames, no further timers. */
  stop(): void {
    this.stopped = true;
    this.manualAttemptInFlight = false;
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    this.retire();
    this.report({ phase: "closed", attempt: this.attempt, permanentReason: null, retryInMs: null });
  }
}

export function isDeviceEvent(value: unknown): value is DeviceEvent {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  if (candidate.schema_version !== 1) return false;
  if (typeof candidate.emitted_at !== "string") return false;
  if (typeof candidate.data !== "object" || candidate.data === null) return false;
  return (
    candidate.type === "telemetry" || candidate.type === "anomaly" || candidate.type === "status"
  );
}
