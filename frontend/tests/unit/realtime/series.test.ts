/** The bounded series: dedupe, order, eviction. */

import { describe, expect, it } from "vitest";

import { MAX_POINTS, attachAnomaly, mergeTelemetry, newestPoint } from "../../../src/realtime/series";
import { stamp, telemetry } from "../../builders";

describe("merging", () => {
  it("keeps rows oldest first", () => {
    const merged = mergeTelemetry([], [telemetry({ id: 3 }), telemetry({ id: 1 }), telemetry({ id: 2 })]);

    expect(merged.series.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(merged.added).toBe(3);
  });

  it("drops a row the series already holds", () => {
    const seeded = mergeTelemetry([], [telemetry({ id: 1 }), telemetry({ id: 2 })]).series;

    const merged = mergeTelemetry(seeded, [telemetry({ id: 2 }), telemetry({ id: 3 })]);

    expect(merged.series.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(merged.added).toBe(1);
    expect(merged.duplicates).toBe(1);
  });

  it("returns the same array when nothing is new, so React does not re-render", () => {
    const seeded = mergeTelemetry([], [telemetry({ id: 1 })]).series;

    const merged = mergeTelemetry(seeded, [telemetry({ id: 1 })]);

    expect(merged.series).toBe(seeded);
    expect(merged.added).toBe(0);
  });

  it("orders by received_at, which is the authoritative order", () => {
    // Ids ascend, but the timestamps do not: the timestamps win.
    const later = telemetry({ id: 1, received_at: stamp(100) });
    const earlier = telemetry({ id: 2, received_at: stamp(50) });

    const merged = mergeTelemetry([], [later, earlier]);

    expect(merged.series.map((row) => row.received_at)).toEqual([stamp(50), stamp(100)]);
  });

  it("breaks a tie on received_at by id, like the backend's own pagination", () => {
    const a = telemetry({ id: 7, received_at: stamp(10) });
    const b = telemetry({ id: 4, received_at: stamp(10) });

    const merged = mergeTelemetry([], [a, b]);

    expect(merged.series.map((row) => row.id)).toEqual([4, 7]);
  });

  it("is bounded: the oldest points go, the newest stay", () => {
    const rows = Array.from({ length: 12 }, (_unused, index) => telemetry({ id: index + 1 }));

    const merged = mergeTelemetry([], rows, 5);

    expect(merged.series).toHaveLength(5);
    expect(merged.series.map((row) => row.id)).toEqual([8, 9, 10, 11, 12]);
    expect(merged.evicted).toBe(7);
  });

  it("defaults to a 600-point buffer", () => {
    const rows = Array.from({ length: MAX_POINTS + 50 }, (_unused, index) =>
      telemetry({ id: index + 1 }),
    );

    const merged = mergeTelemetry([], rows);

    expect(merged.series).toHaveLength(MAX_POINTS);
    expect(newestPoint(merged.series)?.id).toBe(MAX_POINTS + 50);
  });

  it("never evicts the newest point", () => {
    const seeded = mergeTelemetry([], [telemetry({ id: 1 }), telemetry({ id: 2 })], 2).series;

    const merged = mergeTelemetry(seeded, [telemetry({ id: 3 })], 2);

    expect(newestPoint(merged.series)?.id).toBe(3);
  });

  it("reports no newest point for an empty series", () => {
    expect(newestPoint([])).toBeNull();
  });
});

describe("attaching an anomaly", () => {
  const verdict = { id: 9, method: "rule", score: 0.9, model_version: null, reasons: ["r"] };

  it("marks the reading it belongs to", () => {
    const series = mergeTelemetry([], [telemetry({ id: 1 }), telemetry({ id: 2 })]).series;

    const next = attachAnomaly(series, 2, verdict);

    expect(next[1]?.anomaly?.id).toBe(9);
    expect(next[0]?.anomaly).toBeNull();
  });

  it("changes nothing when that reading has already been evicted", () => {
    const series = mergeTelemetry([], [telemetry({ id: 1 })]).series;

    const next = attachAnomaly(series, 404, verdict);

    expect(next).toBe(series);
  });
});
