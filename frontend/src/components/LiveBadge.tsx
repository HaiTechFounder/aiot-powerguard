import type { ReactNode } from "react";

import { formatTimestamp } from "../format/time";
import type { TelemetryDto } from "../api/contract";
import type { LiveState } from "../realtime/liveState";

/**
 * The composite verdict, in one badge.
 *
 * Green is reserved for the case where every condition in `liveState.ts`
 * holds. Everything else names what failed and, when the answer depends on
 * time, says when the last reading actually arrived — an operator reading
 * "Stale data" needs the timestamp to know whether that means seconds or days.
 */
export function LiveBadge({
  state,
  latest,
}: {
  state: LiveState;
  latest: TelemetryDto | null;
}): ReactNode {
  const showLastReceived = !state.isLive && latest !== null;

  return (
    <div className="live-badge-group">
      <span
        className={`live-badge live-badge--${state.tone}`}
        data-testid="live-state"
        data-level={state.level}
        role="status"
      >
        <span className="live-badge__dot" aria-hidden="true" />
        {state.label}
      </span>
      {state.detail ? (
        <span className="live-badge__detail" data-testid="live-state-detail">
          {state.detail}
        </span>
      ) : null}
      {showLastReceived ? (
        <span className="live-badge__detail" data-testid="live-state-last-received">
          Last received {formatTimestamp(latest.received_at)}
        </span>
      ) : null}
    </div>
  );
}
