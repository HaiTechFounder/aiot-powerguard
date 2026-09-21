/** The REST client: success, the error envelope, and everything in between. */

import { afterEach, describe, expect, it, vi } from "vitest";

import { buildHistoryQuery, fetchDevices, fetchTelemetry } from "../../src/api/client";
import { ApiError, isAbort } from "../../src/api/errors";
import { device, telemetryPage } from "../builders";

function respond(body: unknown, init: ResponseInit = {}): Response {
  return new Response(body === null ? "" : JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("history queries", () => {
  it("omits everything that was not asked for", () => {
    expect(buildHistoryQuery()).toBe("");
  });

  it("renders the four supported parameters", () => {
    const query = buildHistoryQuery({
      from: "2026-01-01T00:00:00.000Z",
      to: "2026-01-01T01:00:00.000Z",
      limit: 100,
      before_id: 42,
    });
    const params = new URLSearchParams(query.slice(1));
    expect(params.get("from")).toBe("2026-01-01T00:00:00.000Z");
    expect(params.get("to")).toBe("2026-01-01T01:00:00.000Z");
    expect(params.get("limit")).toBe("100");
    expect(params.get("before_id")).toBe("42");
  });

  it.each([0, 5001, 1.5])("refuses a limit of %s before calling the backend", (limit) => {
    expect(() => buildHistoryQuery({ limit })).toThrow(ApiError);
  });

  it.each([0, -1, 2.5])("refuses a before_id of %s", (before_id) => {
    expect(() => buildHistoryQuery({ before_id })).toThrow(ApiError);
  });
});

describe("responses", () => {
  it("returns the parsed body on success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond({ items: [device()] })));

    const page = await fetchDevices();

    expect(page.items).toHaveLength(1);
    expect(page.items[0]?.id).toBe("powerguard-01");
  });

  it("carries the backend's own message out of the error envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        respond(
          { error: { code: "NOT_FOUND", message: "unknown device: ghost", details: null } },
          { status: 404 },
        ),
      ),
    );

    const failure = await fetchTelemetry("ghost").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    const error = failure as ApiError;
    expect(error.kind).toBe("api");
    expect(error.code).toBe("NOT_FOUND");
    expect(error.message).toBe("unknown device: ghost");
    expect(error.isNotFound).toBe(true);
  });

  it("reports an unreachable backend as a network failure, not an API one", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const failure = (await fetchDevices().catch((error: unknown) => error)) as ApiError;

    expect(failure.kind).toBe("network");
    expect(failure.message).toBe("the backend could not be reached");
  });

  it("treats a failure outside the envelope shape as a protocol failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond("<html>502</html>", { status: 502 })));

    const failure = (await fetchDevices().catch((error: unknown) => error)) as ApiError;

    expect(failure.kind).toBe("protocol");
    expect(failure.status).toBe(502);
  });

  it("does not crash on a body that is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("not json", { status: 200 })),
    );

    const failure = (await fetchDevices().catch((error: unknown) => error)) as ApiError;

    expect(failure.kind).toBe("protocol");
  });

  it("treats an empty successful body as a protocol failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 200 })));

    const failure = (await fetchDevices().catch((error: unknown) => error)) as ApiError;

    expect(failure.kind).toBe("protocol");
  });

  it("returns an empty page without inventing rows", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond({ items: [] })));

    const page = await fetchTelemetry("powerguard-01");

    expect(page.items).toEqual([]);
    expect(page.next_before_id).toBeUndefined();
  });

  it("passes pagination through untouched", async () => {
    const rows = telemetryPage(3, 10);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(respond({ items: rows, next_before_id: 10 })),
    );

    const page = await fetchTelemetry("powerguard-01", { limit: 3 });

    expect(page.items).toHaveLength(3);
    expect(page.next_before_id).toBe(10);
  });

  it("lets an abort through as a cancellation, not an outage", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")),
    );
    controller.abort();

    const failure = await fetchDevices({ signal: controller.signal }).catch(
      (error: unknown) => error,
    );

    expect(isAbort(failure)).toBe(true);
    expect(failure).not.toBeInstanceOf(ApiError);
  });

  it("passes a real AbortSignal to fetch and cancels the request", async () => {
    const controller = new AbortController();
    const fetchSpy = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.signal).toBe(controller.signal);
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    });
    // Stubbing fetch directly bypasses the jsdom/MSW shim in tests/setup.ts.
    vi.stubGlobal("fetch", fetchSpy);

    const pending = fetchDevices({ signal: controller.signal });
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("escapes a device id into the path", async () => {
    const spy = vi.fn().mockResolvedValue(respond({ items: [] }));
    vi.stubGlobal("fetch", spy);

    await fetchTelemetry("odd id/../x");

    expect(spy.mock.calls[0]?.[0]).toContain("odd%20id%2F..%2Fx");
  });
});
