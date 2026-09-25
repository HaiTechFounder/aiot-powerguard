/**
 * The wall clock, ticking once a second.
 *
 * The header prints the viewer's local time beside the UTC offset it belongs
 * to, so the two have to move together; a timestamp frozen at first render
 * would be a clock that lies within a minute of page load.
 */

import { useEffect, useState } from "react";

export function useClock(intervalMs = 1000): Date {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);

  return now;
}
