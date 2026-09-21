import type { ReactNode } from "react";

import type { CatchUpPhase } from "../realtime/useDeviceStream";
import type { ConnectionState } from "../realtime/socket";

/**
 * What the live connection is doing, and whether the history can be trusted.
 *
 * They are separate lines because they are separate facts: the socket can be
 * perfectly healthy while the readings missed during the last drop are still
 * unaccounted for. A permanent close is a dead end and says so — retrying 4404
 * forever would be a loop that can never succeed. A retryable drop says when
 * the next attempt is due, and offers to go now.
 */
function ConnectionLine({
  connection,
  onRetry,
}: {
  connection: ConnectionState;
  onRetry: () => void;
}): ReactNode {
  if (connection.permanentReason) {
    return (
      <div className="banner banner--error" role="alert" data-testid="connection-banner">
        Live updates stopped: {connection.permanentReason}. Previously loaded readings remain visible.
      </div>
    );
  }

  if (connection.phase === "reconnecting") {
    const seconds = connection.retryInMs
      ? Math.max(1, Math.round(connection.retryInMs / 1000))
      : null;
    return (
      <div className="banner banner--warn" role="status" data-testid="connection-banner">
        Live connection lost — reconnecting
        {seconds ? ` in ${seconds}s` : ""} (attempt {connection.attempt}). Readings already loaded
        are kept.
        <button type="button" className="button button--inline" onClick={onRetry}>
          Retry now
        </button>
      </div>
    );
  }

  if (connection.phase === "connecting") {
    return (
      <div className="banner banner--muted" role="status" data-testid="connection-banner">
        Opening the live connection…
      </div>
    );
  }

  return (
    <div className="banner banner--ok" role="status" data-testid="connection-banner">
      Live
    </div>
  );
}

/**
 * Whether anything is missing from the chart, in the plainest words available.
 *
 * "This chart may have a gap" is the whole point of tracking catch-up: a
 * continuous line drawn over an unfetched hole is a lie the operator has no
 * way to detect.
 */
function HistoryLine({
  catchUp,
  historyIncomplete,
  onRetry,
}: {
  catchUp: CatchUpPhase;
  historyIncomplete: boolean;
  onRetry: () => void;
}): ReactNode {
  if (catchUp === "failed") {
    return (
      <div className="banner banner--warn" role="status" data-testid="history-banner">
        The readings missed while offline could not be fetched, so this chart may have a gap.
        Retrying automatically.
        <button type="button" className="button button--inline" onClick={onRetry}>
          Retry now
        </button>
      </div>
    );
  }

  if (catchUp === "truncated") {
    return (
      <div className="banner banner--warn" role="status" data-testid="history-banner">
        More readings were missed than this view keeps. The newest are shown; the older missed
        readings are not, and are still available over the API.
      </div>
    );
  }

  if (catchUp === "running") {
    return (
      <div
        className={historyIncomplete ? "banner banner--warn" : "banner banner--muted"}
        role="status"
        data-testid="history-banner"
      >
        Catching up on readings missed while offline…
        {historyIncomplete ? " This chart may still have a gap until recovery succeeds." : ""}
      </div>
    );
  }

  return null;
}

export function ConnectionBanner({
  connection,
  catchUp,
  historyIncomplete,
  onRetry,
}: {
  connection: ConnectionState;
  catchUp: CatchUpPhase;
  historyIncomplete: boolean;
  onRetry: () => void;
}): ReactNode {
  return (
    <div className="banner-stack">
      <ConnectionLine connection={connection} onRetry={onRetry} />
      <HistoryLine catchUp={catchUp} historyIncomplete={historyIncomplete} onRetry={onRetry} />
    </div>
  );
}
