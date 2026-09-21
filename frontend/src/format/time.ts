/**
 * Timestamps, rendered honestly.
 *
 * Everything the backend sends is UTC. Showing it in the browser's zone is
 * what an operator wants, but relabelling UTC as local without saying so is
 * how people misread an incident timeline — so the offset is always visible.
 */

/** The viewer's UTC offset, e.g. `UTC+07:00`. */
export function localOffsetLabel(at: Date = new Date()): string {
  const minutes = -at.getTimezoneOffset();
  const sign = minutes >= 0 ? "+" : "-";
  const absolute = Math.abs(minutes);
  const hh = String(Math.floor(absolute / 60)).padStart(2, "0");
  const mm = String(absolute % 60).padStart(2, "0");
  return `UTC${sign}${hh}:${mm}`;
}

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const at = new Date(value);
  return Number.isNaN(at.getTime()) ? null : at;
}

/** Local date and time, to the second. */
export function formatTimestamp(value: string | null | undefined): string {
  const at = parse(value);
  if (!at) return "—";
  return at.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** Local clock time only, for a chart axis where the date is implied. */
export function formatClock(value: string | null | undefined): string {
  const at = parse(value);
  if (!at) return "—";
  return at.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** How long ago, in words an operator reads at a glance. */
export function formatAge(value: string | null | undefined, now: Date = new Date()): string {
  const at = parse(value);
  if (!at) return "—";
  const seconds = Math.round((now.getTime() - at.getTime()) / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
