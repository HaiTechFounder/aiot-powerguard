/** Units, decimals and timestamps — the places a dashboard quietly misleads. */

import { describe, expect, it } from "vitest";

import { formatAge, formatClock, formatTimestamp, localOffsetLabel } from "../../src/format/time";
import { formatMetric, formatScore, formatValue } from "../../src/format/units";

describe("metrics", () => {
  it("keeps the precision the device actually sent", () => {
    expect(formatMetric(7.8401, "voltage")).toBe("7.840 V");
    expect(formatMetric(0.417, "current")).toBe("0.417 A");
    expect(formatMetric(3.269, "power")).toBe("3.269 W");
    expect(formatMetric(0.284123, "energy")).toBe("0.284123 Wh");
  });

  it("shows a dash rather than inventing a zero", () => {
    expect(formatMetric(null, "voltage")).toBe("—");
    expect(formatMetric(undefined, "power")).toBe("—");
    expect(formatMetric(Number.NaN, "current")).toBe("—");
    expect(formatMetric(Number.POSITIVE_INFINITY, "current")).toBe("—");
  });

  it("renders a bare value for an axis, where the unit is in the label", () => {
    expect(formatValue(7.84, "voltage")).toBe("7.840");
    expect(formatValue(null, "voltage")).toBe("—");
  });

  it("shows a score to two decimals, or a dash when there is none", () => {
    expect(formatScore(0.9123)).toBe("0.91");
    expect(formatScore(null)).toBe("—");
  });

  it("keeps a negative current, which reverse flow legitimately produces", () => {
    expect(formatMetric(-0.5, "current")).toBe("-0.500 A");
  });
});

describe("timestamps", () => {
  it("states the viewer's offset, so UTC is never silently relabelled", () => {
    expect(localOffsetLabel(new Date("2026-01-01T00:00:00Z"))).toMatch(/^UTC[+-]\d{2}:\d{2}$/);
  });

  it("renders a dash for a missing or unparseable value", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp("not a date")).toBe("—");
    expect(formatClock(undefined)).toBe("—");
    expect(formatAge(null)).toBe("—");
  });

  it("renders a real timestamp", () => {
    expect(formatTimestamp("2026-01-01T10:00:00.000Z")).not.toBe("—");
    expect(formatClock("2026-01-01T10:00:00.000Z")).toMatch(/\d{2}:\d{2}:\d{2}/);
  });

  it("describes age in words an operator reads at a glance", () => {
    const now = new Date("2026-01-01T12:00:00.000Z");
    expect(formatAge("2026-01-01T11:59:57.000Z", now)).toBe("just now");
    expect(formatAge("2026-01-01T11:59:30.000Z", now)).toBe("30s ago");
    expect(formatAge("2026-01-01T11:45:00.000Z", now)).toBe("15m ago");
    expect(formatAge("2026-01-01T09:00:00.000Z", now)).toBe("3h ago");
    expect(formatAge("2025-12-29T12:00:00.000Z", now)).toBe("3d ago");
  });

  it("does not report a future timestamp as a negative age", () => {
    const now = new Date("2026-01-01T12:00:00.000Z");
    expect(formatAge("2026-01-01T12:00:05.000Z", now)).toBe("just now");
  });
});
