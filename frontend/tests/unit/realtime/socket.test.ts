/**
 * The socket's policy: which closes are verdicts, and how the rest are retried.
 */

import { beforeEach, describe, expect, it } from "vitest";

import type { DeviceEvent } from "../../../src/api/contract";
import {
  BACKOFF_CAP_MS,
  DeviceSocket,
  backoffDelayMs,
  describeClose,
  isDeviceEvent,
  isPermanentClose,
} from "../../../src/realtime/socket";
import type { ConnectionState } from "../../../src/realtime/socket";
import { FakeClock, FakeSocket } from "../../fakeSocket";
import { telemetry } from "../../builders";

function build(overrides: Partial<Parameters<typeof makeSocket>[0]> = {}) {
  return makeSocket(overrides);
}

function makeSocket(options: {
  onEvent?: (event: DeviceEvent) => void;
  onOpen?: (info: { reopened: boolean }) => void;
  onState?: (state: ConnectionState) => void;
  random?: () => number;
} = {}) {
  const clock = new FakeClock();
  const states: ConnectionState[] = [];
  const events: DeviceEvent[] = [];
  const opens: boolean[] = [];
  const socket = new DeviceSocket({
    deviceId: "powerguard-01",
    baseUrl: "ws://test",
    onEvent: options.onEvent ?? ((event) => events.push(event)),
    onOpen: options.onOpen ?? ((info) => opens.push(info.reopened)),
    onState: options.onState ?? ((state) => states.push(state)),
    random: options.random ?? (() => 0.5),
    socketFactory: (url) => new FakeSocket(url) as unknown as WebSocket,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  return { socket, clock, states, events, opens };
}

beforeEach(() => {
  FakeSocket.reset();
});

describe("backoff", () => {
  it("grows per attempt and never exceeds the cap", () => {
    const windows = [1000, 2000, 4000, 8000, 16000, 30000, 30000];
    windows.forEach((base, index) => {
      const delay = backoffDelayMs(index + 1, () => 1);
      expect(delay).toBeLessThanOrEqual(Math.min(base, BACKOFF_CAP_MS));
    });
  });

  it("keeps every delay inside the upper half of its window", () => {
    for (let attempt = 1; attempt <= 8; attempt += 1) {
      const low = backoffDelayMs(attempt, () => 0);
      const high = backoffDelayMs(attempt, () => 1);
      const base = Math.min(1000 * 2 ** Math.min(attempt - 1, 5), BACKOFF_CAP_MS);
      expect(low).toBe(base / 2);
      expect(high).toBe(base);
      expect(high).toBeLessThanOrEqual(BACKOFF_CAP_MS);
    }
  });

  it("jitters: the same attempt with different draws differs", () => {
    expect(backoffDelayMs(4, () => 0.1)).not.toBe(backoffDelayMs(4, () => 0.9));
  });
});

describe("close codes", () => {
  it.each([4400, 4404])("treats %s as permanent", (code) => {
    expect(isPermanentClose(code)).toBe(true);
    expect(describeClose(code)).toMatch(/device/);
  });

  it.each([1000, 1006, 1013])("treats %s as retryable", (code) => {
    expect(isPermanentClose(code)).toBe(false);
  });
});

describe("a live socket", () => {
  it("reports open and forwards contract-shaped frames", () => {
    const { socket, states, events } = build();
    socket.start();
    FakeSocket.latest.open();

    const frame: DeviceEvent = {
      schema_version: 1,
      type: "telemetry",
      emitted_at: "2026-01-01T00:00:00.000Z",
      data: telemetry({ id: 5 }),
    };
    FakeSocket.latest.deliver(frame);

    expect(states.map((state) => state.phase)).toEqual(["connecting", "open"]);
    expect(events).toHaveLength(1);
    expect(events[0]?.type).toBe("telemetry");
  });

  it("ignores anything that is not a v1 envelope", () => {
    const { socket, events } = build();
    socket.start();
    FakeSocket.latest.open();

    FakeSocket.latest.deliverRaw("not json");
    FakeSocket.latest.deliverRaw(JSON.stringify({ schema_version: 2, type: "telemetry" }));
    FakeSocket.latest.deliverRaw(JSON.stringify({ hello: "world" }));

    expect(events).toHaveLength(0);
  });
});

describe("closing", () => {
  it.each([4400, 4404])("does not retry after %s", (code) => {
    const { socket, clock, states } = build();
    socket.start();
    FakeSocket.latest.open();

    FakeSocket.latest.serverClose(code);

    expect(clock.pendingCount).toBe(0);
    expect(FakeSocket.instances).toHaveLength(1);
    const last = states.at(-1);
    expect(last?.phase).toBe("closed");
    expect(last?.permanentReason).toBeTruthy();
  });

  it("retries after 1013, which is the backend protecting itself", () => {
    const { socket, clock } = build();
    socket.start();
    FakeSocket.latest.open();

    FakeSocket.latest.serverClose(1013);

    expect(clock.pendingCount).toBe(1);
    clock.runPending();
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("retries after an abnormal network drop", () => {
    const { socket, clock, states } = build();
    socket.start();
    FakeSocket.latest.open();

    FakeSocket.latest.serverClose(1006);

    expect(states.at(-1)?.phase).toBe("reconnecting");
    expect(states.at(-1)?.attempt).toBe(1);
    clock.runPending();
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("grows the delay while it keeps failing and stops at the cap", () => {
    // Each attempt closes without ever opening: a connect that never succeeds.
    const { socket, clock } = build({ random: () => 1 });
    socket.start();

    for (let round = 0; round < 8; round += 1) {
      FakeSocket.latest.serverClose(1006);
      clock.runPending();
    }

    expect(clock.delays[0]).toBe(1000);
    expect(clock.delays[1]).toBe(2000);
    expect(Math.max(...clock.delays)).toBeLessThanOrEqual(BACKOFF_CAP_MS);
    expect(clock.delays.at(-1)).toBe(BACKOFF_CAP_MS);
  });

  it("resets the backoff once a socket opens again", () => {
    const { socket, clock } = build({ random: () => 1 });
    socket.start();
    FakeSocket.latest.open();

    FakeSocket.latest.serverClose(1006);
    clock.runPending();
    FakeSocket.latest.serverClose(1006);
    clock.runPending();
    expect(clock.delays).toEqual([1000, 2000]);

    FakeSocket.latest.open(); // healthy again
    FakeSocket.latest.serverClose(1006);

    expect(clock.delays.at(-1)).toBe(1000);
  });

  it("reports the first open and subsequent reopens", () => {
    const { socket, clock, opens } = build();
    socket.start();
    FakeSocket.latest.open();
    expect(opens).toEqual([false]);

    FakeSocket.latest.serverClose(1006);
    clock.runPending();
    FakeSocket.latest.open();

    expect(opens).toEqual([false, true]);
  });
});

describe("replacement and shutdown", () => {
  it("ignores a frame from a socket that has been replaced", () => {
    const { socket, clock, events } = build();
    socket.start();
    const stale = FakeSocket.latest;
    stale.open();
    stale.serverClose(1006);
    clock.runPending();
    const live = FakeSocket.latest;
    live.open();
    expect(live).not.toBe(stale);

    stale.deliver({
      schema_version: 1,
      type: "telemetry",
      emitted_at: "2026-01-01T00:00:00.000Z",
      data: telemetry({ id: 99 }),
    });

    expect(events).toHaveLength(0);
  });

  it("a replaced socket closing does not schedule another retry", () => {
    const { socket, clock } = build();
    socket.start();
    const stale = FakeSocket.latest;
    stale.open();
    stale.serverClose(1006);
    clock.runPending();
    FakeSocket.latest.open();

    stale.serverClose(1006);

    expect(clock.pendingCount).toBe(0);
  });

  it("stop() closes the socket, cancels the timer and delivers nothing more", () => {
    const { socket, clock, events, states } = build();
    socket.start();
    FakeSocket.latest.open();
    FakeSocket.latest.serverClose(1006);
    expect(clock.pendingCount).toBe(1);

    socket.stop();

    expect(clock.pendingCount).toBe(0);
    expect(states.at(-1)?.phase).toBe("closed");

    clock.runPending();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(events).toHaveLength(0);
  });

  it("stop() closes an open socket exactly once", () => {
    const { socket } = build();
    socket.start();
    const live = FakeSocket.latest;
    live.open();

    socket.stop();

    expect(live.closeCalls).toBe(1);
    expect(live.onmessage).toBeNull();
  });

  it("retryNow replaces the pending timer with an immediate attempt", () => {
    const { socket, clock } = build();
    socket.start();
    FakeSocket.latest.open();
    FakeSocket.latest.serverClose(1006);

    socket.retryNow();

    expect(clock.pendingCount).toBe(0);
    expect(FakeSocket.instances).toHaveLength(2);
  });
});

describe("envelope guard", () => {
  it.each([
    [{ schema_version: 1, type: "telemetry", emitted_at: "x", data: {} }, true],
    [{ schema_version: 1, type: "status", emitted_at: "x", data: {} }, true],
    [{ schema_version: 2, type: "telemetry", emitted_at: "x", data: {} }, false],
    [{ schema_version: 1, type: "other", emitted_at: "x", data: {} }, false],
    [{ schema_version: 1, type: "telemetry", data: {} }, false],
    [null, false],
    ["string", false],
  ])("classifies %o as %s", (value, expected) => {
    expect(isDeviceEvent(value)).toBe(expected);
  });
});
