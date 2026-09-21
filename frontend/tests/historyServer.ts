/**
 * A REST history endpoint that paginates the way the backend does.
 *
 * Catch-up is only interesting when a gap is wider than one page, so a mock
 * that always answers with one array proves nothing. This one holds rows,
 * applies `from` (inclusive), `before_id` (exclusive) and `limit`, answers
 * newest-first, and sets `next_before_id` only when another page may exist —
 * `API_CONTRACT.md` §history.
 *
 * It can also be told to fail a particular page, to overlap pages, or to hold
 * a response open, because those are the situations the hook has to survive.
 */

import type { HistoryQuery, TelemetryDto, TelemetryPageDto } from "../src/api/contract";
import { ApiError } from "../src/api/errors";

interface Pending {
  query: HistoryQuery;
  resolve: (page: TelemetryPageDto) => void;
  reject: (failure: unknown) => void;
}

export class HistoryServer {
  /** Ascending by id, as the database holds them. */
  private rows: TelemetryDto[] = [];
  /** Every query received, in order; the evidence for what catch-up asked. */
  readonly queries: HistoryQuery[] = [];
  /** Page numbers (1-based) that must fail, consumed as they are hit. */
  failPages = new Set<number>();
  /** Repeat this many rows across a page boundary, to exercise dedupe. */
  pageOverlap = 0;
  /** When true, responses wait for `release()` instead of resolving. */
  private held = false;
  private pending: Pending[] = [];
  private calls = 0;

  constructor(rows: readonly TelemetryDto[] = []) {
    this.add(rows);
  }

  add(rows: readonly TelemetryDto[]): void {
    this.rows = [...this.rows, ...rows].sort((a, b) => a.id - b.id);
  }

  hold(): void {
    this.held = true;
  }

  /** Resolve everything held so far and stop holding. */
  release(): void {
    this.held = false;
    const waiting = this.pending;
    this.pending = [];
    for (const entry of waiting) entry.resolve(this.page(entry.query));
  }

  get callCount(): number {
    return this.calls;
  }

  private page(query: HistoryQuery): TelemetryPageDto {
    const limit = query.limit ?? 500;
    let matching = this.rows;
    if (query.from) matching = matching.filter((row) => row.received_at >= (query.from as string));
    if (query.before_id !== undefined) {
      matching = matching.filter((row) => row.id < (query.before_id as number));
    }
    const newestFirst = [...matching].sort((a, b) =>
      a.received_at === b.received_at ? b.id - a.id : a.received_at < b.received_at ? 1 : -1,
    );
    const items = newestFirst.slice(0, limit);
    const remaining = newestFirst.length > items.length;
    const oldest = items.at(-1);
    return {
      items,
      // The overlap deliberately re-serves rows the client already has.
      next_before_id: remaining && oldest ? oldest.id + this.pageOverlap : null,
    };
  }

  fetchTelemetry = (
    _deviceId: string,
    query: HistoryQuery = {},
    _options?: { signal?: AbortSignal },
  ): Promise<TelemetryPageDto> => {
    void _options;
    this.calls += 1;
    this.queries.push(query);
    if (this.failPages.has(this.calls)) {
      return Promise.reject(new ApiError("network", "the backend could not be reached"));
    }
    if (this.held) {
      return new Promise<TelemetryPageDto>((resolve, reject) => {
        this.pending.push({ query, resolve, reject });
      });
    }
    return Promise.resolve(this.page(query));
  };
}
