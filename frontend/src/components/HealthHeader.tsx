import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { HealthDto } from "../api/contract";
import type { ApiError } from "../api/errors";
import { localOffsetLabel } from "../format/time";

function tone(label: string, good: readonly string[]): string {
  return good.includes(label) ? "ok" : "warn";
}

/**
 * Backend, broker and model, side by side and always visible.
 *
 * A broker outage and an absent model are *normal* conditions here: history
 * stays readable without MQTT, and Phase 03 ships no model at all. They are
 * shown as states, not as alarms.
 */
export function HealthHeader({
  health,
  error,
}: {
  health: HealthDto | null;
  error: ApiError | null;
}): ReactNode {
  return (
    <header className="header">
      <Link to="/" className="header__brand">
        AIoT PowerGuard
      </Link>

      <div className="header__health" data-testid="health-header">
        {error ? (
          <span className="chip chip--warn" data-testid="health-backend">
            Backend unreachable
          </span>
        ) : health ? (
          <>
            <span className={`chip chip--${tone(health.status, ["ok"])}`} data-testid="health-backend">
              Backend {health.status}
            </span>
            <span
              className={`chip chip--${tone(health.database, ["ready"])}`}
              data-testid="health-database"
            >
              Database {health.database}
            </span>
            <span
              className={`chip chip--${tone(health.mqtt, ["connected"])}`}
              data-testid="health-mqtt"
              title="A broker outage does not stop history from being readable."
            >
              Broker {health.mqtt}
            </span>
            <span
              className={`chip chip--${tone(health.model, ["ready"])}`}
              data-testid="health-model"
              title="No model is shipped in this phase; anomaly detection is unavailable."
            >
              Detection {health.model === "ready" ? "ready" : "unavailable"}
            </span>
            <span className="chip chip--muted">v{health.version}</span>
          </>
        ) : (
          <span className="chip chip--muted" data-testid="health-backend">
            Checking…
          </span>
        )}
        <span className="chip chip--muted" title="All timestamps are shown in this zone.">
          {localOffsetLabel()}
        </span>
      </div>
    </header>
  );
}
