/**
 * A WebSocket the test drives.
 *
 * The production socket only ever uses `onopen`, `onmessage`, `onclose`,
 * `onerror` and `close()`, so that is all this provides. Every instance is
 * recorded, which is how a test proves a *replaced* socket can no longer
 * change anything.
 */

import type { DeviceEvent } from "../src/api/contract";

export class FakeSocket {
  static instances: FakeSocket[] = [];

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  closeCalls = 0;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  static reset(): void {
    FakeSocket.instances = [];
  }

  static get latest(): FakeSocket {
    const socket = FakeSocket.instances.at(-1);
    if (!socket) throw new Error("no socket was opened");
    return socket;
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  deliver(event: DeviceEvent): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
  }

  deliverRaw(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  serverClose(code: number): void {
    this.closed = true;
    this.onclose?.(new CloseEvent("close", { code }));
  }

  close(): void {
    this.closeCalls += 1;
    this.closed = true;
  }
}

/** A controllable timer pair, so backoff is asserted rather than waited out. */
export class FakeClock {
  private handle = 1;
  private readonly pending = new Map<number, () => void>();
  readonly delays: number[] = [];

  setTimer = (fn: () => void, ms: number): number => {
    const id = this.handle++;
    this.delays.push(ms);
    this.pending.set(id, fn);
    return id;
  };

  clearTimer = (id: number): void => {
    this.pending.delete(id);
  };

  get pendingCount(): number {
    return this.pending.size;
  }

  /** Fire every timer currently scheduled. */
  runPending(): void {
    const entries = [...this.pending.entries()];
    this.pending.clear();
    for (const [, fn] of entries) fn();
  }
}
