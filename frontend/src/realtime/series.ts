/**
 * The bounded telemetry series a device view holds.
 *
 * REST is the truth and the socket is a live tail, so both feed the same
 * buffer and both go through the same door. Rows carry a database id, which is
 * what makes that safe: a row already held is dropped, whether it arrived
 * twice on the socket or once on the socket and again in a backfill.
 *
 * `received_at` is the authoritative order (ADR-006) and ids are assigned in
 * that order, so ordering by id and ordering by `received_at` agree. Ties on
 * `received_at` are broken by id, exactly as the backend's own pagination does.
 */

import type { TelemetryDto } from "../api/contract";

/** About twenty minutes at the firmware's two-second cadence. */
export const MAX_POINTS = 600;

export interface MergeResult {
  series: TelemetryDto[];
  /** Rows that were genuinely new. */
  added: number;
  /** Rows dropped because the series already held them. */
  duplicates: number;
  /** Rows dropped because the buffer is bounded. */
  evicted: number;
}

function compare(a: TelemetryDto, b: TelemetryDto): number {
  if (a.received_at === b.received_at) return a.id - b.id;
  return a.received_at < b.received_at ? -1 : 1;
}

/**
 * Merge rows into a series, oldest first, without duplicates.
 *
 * The incoming rows may be in any order and may overlap what is already held —
 * a backfill after a reconnect usually does.
 */
export function mergeTelemetry(
  current: readonly TelemetryDto[],
  incoming: readonly TelemetryDto[],
  maxPoints: number = MAX_POINTS,
): MergeResult {
  if (incoming.length === 0) {
    return { series: current as TelemetryDto[], added: 0, duplicates: 0, evicted: 0 };
  }

  const byId = new Map<number, TelemetryDto>();
  for (const row of current) byId.set(row.id, row);

  let added = 0;
  let duplicates = 0;
  for (const row of incoming) {
    if (byId.has(row.id)) {
      duplicates += 1;
      continue;
    }
    byId.set(row.id, row);
    added += 1;
  }

  if (added === 0) {
    return { series: current as TelemetryDto[], added: 0, duplicates, evicted: 0 };
  }

  const merged = [...byId.values()].sort(compare);
  // The newest points are the ones worth keeping: drop from the front.
  const evicted = Math.max(0, merged.length - maxPoints);
  return { series: evicted ? merged.slice(evicted) : merged, added, duplicates, evicted };
}

/** The newest row held, or null when the series is empty. */
export function newestPoint(series: readonly TelemetryDto[]): TelemetryDto | null {
  return series.length ? (series[series.length - 1] as TelemetryDto) : null;
}

/**
 * Attach an anomaly to the reading it belongs to, if that reading is held.
 *
 * A verdict for a row that has already been evicted changes nothing here; the
 * anomaly list keeps it either way.
 */
export function attachAnomaly(
  series: readonly TelemetryDto[],
  telemetryId: number,
  anomaly: NonNullable<TelemetryDto["anomaly"]>,
): TelemetryDto[] {
  let touched = false;
  const next = series.map((row) => {
    if (row.id !== telemetryId) return row;
    touched = true;
    return { ...row, anomaly };
  });
  return touched ? next : (series as TelemetryDto[]);
}
