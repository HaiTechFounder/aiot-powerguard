/**
 * Let asynchronous work a test started finish inside `act`.
 *
 * Opening a socket or pressing Retry Now starts a REST catch-up whose promise
 * resolves after the synchronous `act` that triggered it has closed. The
 * assertions that follow are about the moment *before* that catch-up lands, and
 * must stay that way; awaiting this at the end of such a test lets the tail
 * commit inside `act` instead of leaking out as a warning.
 */

import { act } from "@testing-library/react";

export async function settle(): Promise<void> {
  await act(async () => {
    // One macrotask drains every microtask queued behind the mocked fetch.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}
