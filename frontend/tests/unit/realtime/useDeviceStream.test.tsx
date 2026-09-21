/**
 * Seed, tail, and the gap between them.
 *
 * These are the cases that decide whether an operator is looking at a complete
 * picture or a chart with a hole in it, so each is driven end to end through
 * the hook against a REST history that paginates the way the backend does.
 *
 * The rule under test throughout: the dashboard may show an incomplete
 * history, but it may never present one as complete.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../../src/api/client";
import type { DeviceEvent, TelemetryDto, TelemetryPageDto } from "../../../src/api/contract";
import { ApiError } from "../../../src/api/errors";
import { useDeviceStream } from "../../../src/realtime/useDeviceStream";
import { DEVICE_ID, anomaly, stamp, telemetry, telemetryPage } from "../../builders";
import { FakeClock, FakeSocket } from "../../fakeSocket";
import { HistoryServer } from "../../historyServer";

function frame(row: TelemetryDto): DeviceEvent {
  return { schema_version: 1, type: "telemetry", emitted_at: row.received_at, data: row };
}

/** `count` rows starting at `startId`, ascending — the database's own order. */
function rows(count: number, startId = 1): TelemetryDto[] {
  return Array.from({ length: count }, (_unused, index) => telemetry({ id: startId + index }));
}

function makeClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    fetchHealth: vi.fn(),
    fetchDevices: vi.fn(),
    fetchLatest: vi.fn(),
    fetchTelemetry: vi.fn().mockResolvedValue({ items: [] }),
    fetchAnomalies: vi.fn(),
    ...overrides,
  } as ApiClient;
}

interface RenderOptions {
  clock?: FakeClock;
  maxPoints?: number;
  limit?: number;
  deviceId?: string;
}

function render(client: ApiClient, options: RenderOptions = {}) {
  const clock = options.clock ?? new FakeClock();
  const socket = {
    baseUrl: "ws://test",
    random: () => 0.5,
    socketFactory: (url: string) => new FakeSocket(url) as unknown as WebSocket,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  };
  const view = renderHook(
    ({ deviceId }: { deviceId: string }) =>
      useDeviceStream(deviceId, {
        client,
        limit: options.limit ?? 50,
        maxPoints: options.maxPoints,
        socket,
      }),
    { initialProps: { deviceId: options.deviceId ?? DEVICE_ID } },
  );
  return { ...view, clock };
}

/** The live sockets — retired ones are closed and must not be counted. */
function liveSockets(): FakeSocket[] {
  return FakeSocket.instances.filter((socket) => !socket.closed);
}

beforeEach(() => {
  FakeSocket.reset();
});

describe("seeding", () => {
  it("loads history newest-first and shows it oldest-first", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(3) }),
    });

    const { result } = render(client);

    await waitFor(() => expect(result.current.seed).toBe("ready"));
    expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3]);
  });

  it("reports a seed failure but still opens the socket", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockRejectedValue(new ApiError("network", "backend is down")),
    });

    const { result } = render(client);

    await waitFor(() => expect(result.current.seed).toBe("failed"));
    expect(result.current.error?.message).toBe("backend is down");
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("treats an empty history as empty, not as an error", async () => {
    const { result } = render(makeClient());

    await waitFor(() => expect(result.current.seed).toBe("ready"));
    expect(result.current.series).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it("recovers a failed seed on the first catch-up instead of staying broken", async () => {
    const server = new HistoryServer(rows(3));
    const fetchTelemetry = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("network", "backend is down"))
      .mockImplementation(server.fetchTelemetry);
    const { result } = render(makeClient({ fetchTelemetry }));
    await waitFor(() => expect(result.current.seed).toBe("failed"));

    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.seed).toBe("ready"));
    expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(result.current.error).toBeNull();
  });
});

describe("the live tail", () => {
  it("appends frames after the seed", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(2) }),
    });
    const { result } = render(client);
    await waitFor(() => expect(result.current.series).toHaveLength(2));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.deliver(frame(telemetry({ id: 3 })));
    });

    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3]));
  });

  it("drops a frame the series already holds", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(2) }),
    });
    const { result } = render(client);
    await waitFor(() => expect(result.current.series).toHaveLength(2));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.deliver(frame(telemetry({ id: 2 })));
      FakeSocket.latest.deliver(frame(telemetry({ id: 2 })));
    });

    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    expect(result.current.series).toHaveLength(2);
  });

  it("stays bounded as frames arrive", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(3) }),
    });
    const { result } = render(client, { maxPoints: 3 });
    await waitFor(() => expect(result.current.series).toHaveLength(3));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.deliver(frame(telemetry({ id: 4 })));
      FakeSocket.latest.deliver(frame(telemetry({ id: 5 })));
    });

    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([3, 4, 5]));
  });

  it("orders a tie on received_at by id, whichever arrives first", async () => {
    const early = telemetry({ id: 7, received_at: stamp(100) });
    const late = telemetry({ id: 8, received_at: stamp(100) });
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: [] }),
    });
    const { result } = render(client);
    await waitFor(() => expect(result.current.seed).toBe("ready"));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.deliver(frame(late));
      FakeSocket.latest.deliver(frame(early));
    });

    await waitFor(() => expect(result.current.series).toHaveLength(2));
    expect(result.current.series.map((row) => row.id)).toEqual([7, 8]);
  });

  it("records a status frame", async () => {
    const { result } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.deliver({
        schema_version: 1,
        type: "status",
        emitted_at: stamp(1),
        data: { device_id: DEVICE_ID, status: "stale", last_seen_at: stamp(1) },
      });
    });

    expect(result.current.status).toBe("stale");
  });

  it("marks the reading an anomaly belongs to and lists the verdict", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(2) }),
    });
    const { result } = render(client);
    await waitFor(() => expect(result.current.series).toHaveLength(2));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.deliver({
        schema_version: 1,
        type: "anomaly",
        emitted_at: stamp(4),
        data: anomaly({ id: 11, telemetry_id: 2 }),
      });
    });

    expect(result.current.anomalies.map((item) => item.id)).toEqual([11]);
    expect(result.current.series[1]?.anomaly?.id).toBe(11);
  });
});

describe("the first open", () => {
  it("lets a new socket opening supersede an older in-flight backfill", async () => {
    let oldResolve: ((page: TelemetryPageDto) => void) | undefined;
    let replacementResolve: ((page: TelemetryPageDto) => void) | undefined;
    let calls = 0;
    const fetchTelemetry = vi.fn(() => {
      calls += 1;
      if (calls === 1) return Promise.resolve({ items: [telemetry({ id: 1 })] });
      if (calls === 2) {
        return new Promise<TelemetryPageDto>((resolve) => {
          oldResolve = resolve;
        });
      }
      return new Promise<TelemetryPageDto>((resolve) => {
        replacementResolve = resolve;
      });
    });
    const { result, clock } = render(makeClient({ fetchTelemetry }));
    await waitFor(() => expect(result.current.seed).toBe("ready"));

    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(oldResolve).toBeDefined());
    act(() => FakeSocket.latest.serverClose(1006));
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(replacementResolve).toBeDefined());

    await act(async () => {
      replacementResolve?.({ items: [telemetry({ id: 3 }), telemetry({ id: 2 }), telemetry({ id: 1 })] });
    });
    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3]));

    await act(async () => {
      oldResolve?.({ items: [telemetry({ id: 1 })] });
    });
    expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(result.current.catchUp).toBe("complete");
    expect(result.current.historyIncomplete).toBe(false);
  });

  it("fetches readings committed between the snapshot and the subscription", async () => {
    // The race the seed alone cannot close: row 3 is written after REST
    // answered and before the socket is listening, so nothing would ever
    // deliver it if catch-up only ran on *re*-open.
    const server = new HistoryServer(rows(2));
    const { result } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }));
    await waitFor(() => expect(result.current.series).toHaveLength(2));

    server.add(rows(1, 3));
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3]));
    expect(result.current.catchUp).toBe("complete");
    expect(result.current.historyIncomplete).toBe(false);
  });

  it("asks from the newest row it holds, so the overlap can prove coverage", async () => {
    const server = new HistoryServer(rows(2));
    const { result } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }));
    await waitFor(() => expect(result.current.series).toHaveLength(2));

    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    expect(server.queries[1]).toMatchObject({ from: telemetry({ id: 2 }).received_at });
  });

  it("holds live frames until catch-up finishes, then merges them in order", async () => {
    const server = new HistoryServer(rows(2));
    const { result } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }));
    await waitFor(() => expect(result.current.series).toHaveLength(2));

    server.add(rows(1, 3));
    server.hold();
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("running"));

    // Arrives while REST is still answering: queued, never dropped.
    act(() => FakeSocket.latest.deliver(frame(telemetry({ id: 4 }))));
    expect(result.current.series.map((row) => row.id)).toEqual([1, 2]);

    await act(async () => {
      server.release();
    });

    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([1, 2, 3, 4]));
  });
});

describe("a gap wider than one page", () => {
  it("follows next_before_id until the anchor row comes back", async () => {
    const server = new HistoryServer(rows(10));
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }), {
      limit: 50,
      maxPoints: 600,
    });
    await waitFor(() => expect(result.current.series).toHaveLength(10));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    // 130 readings while the socket was down: three pages of 50, not one.
    act(() => FakeSocket.latest.serverClose(1006));
    server.add(rows(130, 11));
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    expect(result.current.catchUpPages).toBe(3);
    expect(result.current.series).toHaveLength(140);
    expect(result.current.series.map((row) => row.id)).toEqual(
      Array.from({ length: 140 }, (_unused, index) => index + 1),
    );
    expect(result.current.historyIncomplete).toBe(false);
  });

  it("dedupes rows that appear on both sides of a page boundary", async () => {
    const server = new HistoryServer(rows(10));
    server.pageOverlap = 3; // every page repeats three rows of the last
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }), {
      limit: 20,
      maxPoints: 600,
    });
    await waitFor(() => expect(result.current.series).toHaveLength(10));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    act(() => FakeSocket.latest.serverClose(1006));
    server.add(rows(60, 11));
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    const ids = result.current.series.map((row) => row.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toEqual(Array.from({ length: 70 }, (_unused, index) => index + 1));
  });

  it("keeps the newest window and says so when the gap outgrows it", async () => {
    const server = new HistoryServer(rows(10));
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }), {
      limit: 10,
      maxPoints: 20,
    });
    await waitFor(() => expect(result.current.series).toHaveLength(10));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    act(() => FakeSocket.latest.serverClose(1006));
    server.add(rows(100, 11)); // 100 missed, 20 retainable
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).toBe("truncated"));
    // Truthful: the newest 20, and an explicit admission that the rest are gone.
    expect(result.current.series).toHaveLength(20);
    expect(result.current.series.at(0)?.id).toBe(91);
    expect(result.current.series.at(-1)?.id).toBe(110);
    expect(result.current.historyIncomplete).toBe(true);
  });

  it("stays bounded when a multi-page catch-up overfills the buffer", async () => {
    const server = new HistoryServer(rows(5));
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }), {
      limit: 10,
      maxPoints: 25,
    });
    await waitFor(() => expect(result.current.series).toHaveLength(5));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    act(() => FakeSocket.latest.serverClose(1006));
    server.add(rows(40, 6));
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).not.toBe("running"));
    expect(result.current.series.length).toBeLessThanOrEqual(25);
    expect(result.current.series.at(-1)?.id).toBe(45); // the newest is never evicted
  });

  it("does not claim success when page overlap exhausts the bounded query budget", async () => {
    const server = new HistoryServer(rows(10));
    server.pageOverlap = 9;
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }), {
      limit: 10,
      maxPoints: 20,
    });
    await waitFor(() => expect(result.current.series).toHaveLength(10));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    act(() => FakeSocket.latest.serverClose(1006));
    server.add(rows(100, 11));
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).toBe("failed"));
    expect(result.current.historyIncomplete).toBe(true);
  });
});

describe("a catch-up that fails", () => {
  it("does not claim recovery, and retries from the original anchor", async () => {
    const server = new HistoryServer(rows(10));
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }), {
      limit: 50,
      maxPoints: 600,
    });
    await waitFor(() => expect(result.current.series).toHaveLength(10));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    act(() => FakeSocket.latest.serverClose(1006));
    server.add(rows(130, 11));
    server.failPages = new Set([4]); // the second page of the catch-up
    act(() => clock.runPending());
    act(() => FakeSocket.latest.open());

    await waitFor(() => expect(result.current.catchUp).toBe("failed"));
    expect(result.current.historyIncomplete).toBe(true);
    // Partial rows are kept — they are real — but completeness is not claimed.
    expect(result.current.series.length).toBeLessThan(140);

    // The retry starts from where the last *proven* row was, not from the
    // newest row the failed attempt happened to fetch.
    act(() => clock.runPending());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    expect(server.queries.at(-1)?.before_id).toBeDefined();
    expect(server.queries.filter((query) => query.from === telemetry({ id: 10 }).received_at))
      .not.toHaveLength(0);
    expect(result.current.series).toHaveLength(140);
    expect(result.current.historyIncomplete).toBe(false);
  });

  it("retries on its own timer, without needing the socket to drop again", async () => {
    const server = new HistoryServer(rows(4));
    server.failPages = new Set([2]);
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }));
    await waitFor(() => expect(result.current.series).toHaveLength(4));

    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("failed"));
    expect(clock.pendingCount).toBe(1);

    act(() => clock.runPending());

    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    // One socket throughout: the history retry is not a reconnect.
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("keeps the warning up until a retry actually succeeds", async () => {
    const server = new HistoryServer(rows(4));
    server.failPages = new Set([2, 3]);
    const { result, clock } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }));
    await waitFor(() => expect(result.current.series).toHaveLength(4));
    act(() => FakeSocket.latest.open());
    await waitFor(() => expect(result.current.catchUp).toBe("failed"));

    act(() => clock.runPending());
    await waitFor(() => expect(result.current.catchUp).toBe("failed"));
    expect(result.current.historyIncomplete).toBe(true);

    server.hold();
    act(() => clock.runPending());
    await waitFor(() => expect(result.current.catchUp).toBe("running"));
    expect(result.current.historyIncomplete).toBe(true);
    await act(async () => server.release());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));
    expect(result.current.historyIncomplete).toBe(false);
  });

  it("keeps the series when the socket drops", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(3) }),
    });
    const { result } = render(client);
    await waitFor(() => expect(result.current.series).toHaveLength(3));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.serverClose(1013);
    });

    expect(result.current.series).toHaveLength(3);
    expect(result.current.connection.phase).toBe("reconnecting");
  });
});

describe("one stream owns one socket", () => {
  it("replaces the pending reconnect rather than adding to it", async () => {
    const { result, clock } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));
    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.serverClose(1006);
    });
    expect(clock.pendingCount).toBe(1);

    act(() => result.current.retryNow());

    expect(clock.pendingCount).toBe(0);
    expect(liveSockets()).toHaveLength(1);
  });

  it("supersedes a connection that has not opened yet", async () => {
    const { result } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));
    const connecting = FakeSocket.latest;

    act(() => result.current.retryNow());

    expect(connecting.closed).toBe(true);
    expect(liveSockets()).toHaveLength(1);
  });

  it("does not storm the backend when the button is pressed repeatedly", async () => {
    const { result } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));

    act(() => {
      result.current.retryNow();
      result.current.retryNow();
      result.current.retryNow();
      result.current.retryNow();
    });

    expect(liveSockets()).toHaveLength(1);
    expect(FakeSocket.instances).toHaveLength(2);
    expect(FakeSocket.instances.filter((socket) => socket.closed)).toHaveLength(1);
  });

  it("does not replace a healthy socket on Retry Now", async () => {
    const { result } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));
    const live = FakeSocket.latest;
    act(() => live.open());
    await waitFor(() => expect(result.current.catchUp).toBe("complete"));

    act(() => result.current.retryNow());

    expect(FakeSocket.instances).toHaveLength(1);
    expect(live.closed).toBe(false);
  });

  it("ignores a frame from the socket a manual retry replaced", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(1) }),
    });
    const { result } = render(client);
    await waitFor(() => expect(result.current.series).toHaveLength(1));
    const stale = FakeSocket.latest;

    act(() => result.current.retryNow());
    act(() => {
      stale.open();
      stale.deliver(frame(telemetry({ id: 99 })));
      stale.serverClose(1006);
    });

    expect(result.current.series.map((row) => row.id)).toEqual([1]);
    expect(liveSockets()).toHaveLength(1);
  });

  it("leaves nothing behind when the view unmounts mid-retry", async () => {
    const { result, unmount, clock } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));
    act(() => result.current.retryNow());

    unmount();

    expect(liveSockets()).toHaveLength(0);
    expect(clock.pendingCount).toBe(0);
  });
});

describe("permanent closes", () => {
  it.each([4400, 4404])("stops for good on %s", async (code) => {
    const { result, clock } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));

    act(() => FakeSocket.latest.serverClose(code));

    expect(result.current.connection.permanentReason).toBeTruthy();
    expect(clock.pendingCount).toBe(0);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});

describe("switching device", () => {
  it("ignores a response for the device the operator has left", async () => {
    const alpha = new HistoryServer(rows(2));
    const beta = new HistoryServer([telemetry({ id: 500 })]);
    const fetchTelemetry = vi.fn((deviceId: string, query = {}, options = undefined) =>
      deviceId === "alpha"
        ? alpha.fetchTelemetry(deviceId, query, options)
        : beta.fetchTelemetry(deviceId, query, options),
    );
    alpha.hold();

    const { result, rerender } = render(makeClient({ fetchTelemetry }), { deviceId: "alpha" });
    rerender({ deviceId: "beta" });
    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([500]));

    // Alpha answers after the operator has moved on.
    await act(async () => {
      alpha.release();
    });

    expect(result.current.series.map((row) => row.id)).toEqual([500]);
  });

  it("does not resurrect the previous device's history on a slow page", async () => {
    const alpha = new HistoryServer(rows(3));
    const beta = new HistoryServer([telemetry({ id: 900 })]);
    const fetchTelemetry = vi.fn((deviceId: string, query = {}, options = undefined) =>
      deviceId === "alpha"
        ? alpha.fetchTelemetry(deviceId, query, options)
        : beta.fetchTelemetry(deviceId, query, options),
    );

    const { result, rerender } = render(makeClient({ fetchTelemetry }), { deviceId: "alpha" });
    await waitFor(() => expect(result.current.series).toHaveLength(3));

    alpha.hold();
    act(() => FakeSocket.latest.open()); // alpha's catch-up starts and hangs
    rerender({ deviceId: "beta" });
    await waitFor(() => expect(result.current.series.map((row) => row.id)).toEqual([900]));

    await act(async () => {
      alpha.release();
    });

    expect(result.current.series.map((row) => row.id)).toEqual([900]);
    expect(result.current.catchUp).not.toBe("running");
  });
});

describe("unmounting", () => {
  it("closes the socket and cancels every timer", async () => {
    const { result, unmount, clock } = render(makeClient());
    await waitFor(() => expect(result.current.seed).toBe("ready"));
    const live = FakeSocket.latest;
    act(() => {
      live.open();
      live.serverClose(1006);
    });
    expect(clock.pendingCount).toBe(1);

    unmount();

    expect(clock.pendingCount).toBe(0);
    act(() => clock.runPending());
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("a frame arriving after unmount changes nothing", async () => {
    const client = makeClient({
      fetchTelemetry: vi.fn().mockResolvedValue({ items: telemetryPage(1) }),
    });
    const { result, unmount } = render(client);
    await waitFor(() => expect(result.current.series).toHaveLength(1));
    const live = FakeSocket.latest;
    act(() => live.open());

    unmount();
    act(() => live.deliver(frame(telemetry({ id: 99 }))));

    expect(result.current.series.map((row) => row.id)).toEqual([1]);
  });

  it("a REST page arriving after unmount changes nothing", async () => {
    const server = new HistoryServer(rows(2));
    const { result, unmount } = render(makeClient({ fetchTelemetry: server.fetchTelemetry }));
    await waitFor(() => expect(result.current.series).toHaveLength(2));
    server.add(rows(5, 3));
    server.hold();
    act(() => FakeSocket.latest.open());

    unmount();
    await act(async () => {
      server.release();
    });

    expect(result.current.series).toHaveLength(2);
  });
});
