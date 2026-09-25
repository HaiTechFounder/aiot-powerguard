import type { ReactNode } from "react";

import type { HealthDto } from "../api/contract";
import type { ApiError } from "../api/errors";
import { localOffsetLabel } from "../format/time";
import { useClock } from "../hooks/useClock";
import { ClockIcon } from "./icons";

function tone(label: string, good: readonly string[]): string {
  return good.includes(label) ? "ok" : "warn";
}

function Stat({
  label,
  value,
  ok,
  testId,
  title,
  divider,
}: {
  label: string;
  value: string;
  ok: boolean;
  testId: string;
  title?: string;
  divider?: boolean;
}): ReactNode {
  return (
    <span
      className={`chip chip--${ok ? "ok" : "warn"}${divider ? " chip--divider" : ""}`}
      data-testid={testId}
      title={title}
    >
      <span className="chip__label">{label}</span>{" "}
      <span className="chip__value">{value}</span>
    </span>
  );
}

/**
 * The page's masthead: what this is, and what the system is currently doing.
 *
 * Backend, database, broker and model are read straight off `/health` and
 * printed as they came. A broker outage and an absent model are *normal*
 * conditions here — history stays readable without MQTT, and Phase 03 ships no
 * model at all — so they are shown as states, not rounded up to green and not
 * raised as alarms.
 */
export function HealthHeader({
  health,
  error,
}: {
  health: HealthDto | null;
  error: ApiError | null;
}): ReactNode {
  const now = useClock();

  return (
    <header className="topbar">
      <div className="topbar__titles">
        <h1 className="topbar__title">PowerGuard</h1>
        <p className="topbar__subtitle">Monitor today. Safer tomorrow.</p>
      </div>

      <div className="topbar__aside">
        <div className="health-panel" data-testid="health-header">
          {error ? (
            <Stat label="Backend" value="unreachable" ok={false} testId="health-backend" />
          ) : health ? (
            <>
              <Stat
                label="Backend"
                value={health.status}
                ok={tone(health.status, ["ok"]) === "ok"}
                testId="health-backend"
              />
              <Stat
                label="Database"
                value={health.database}
                ok={tone(health.database, ["ready"]) === "ok"}
                testId="health-database"
                divider
              />
              <Stat
                label="MQTT"
                value={health.mqtt}
                ok={tone(health.mqtt, ["connected"]) === "ok"}
                testId="health-mqtt"
                title="A broker outage does not stop history from being readable."
                divider
              />
              <Stat
                label="Model"
                value={health.model === "ready" ? "ready" : "unavailable"}
                ok={tone(health.model, ["ready"]) === "ok"}
                testId="health-model"
                title="No model is shipped in this phase; anomaly detection is unavailable."
                divider
              />
            </>
          ) : (
            <span className="chip" data-testid="health-backend">
              <span className="chip__label">Backend</span>{" "}
              <span className="chip__value">checking…</span>
            </span>
          )}
        </div>

        <div className="clock" data-testid="local-clock">
          <ClockIcon size={17} />
          <span className="clock__time">
            {now.toLocaleTimeString(undefined, {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
              hour12: false,
            })}
          </span>
          <span className="clock__zone" title="All timestamps are shown in this zone.">
            {localOffsetLabel(now)}
          </span>
        </div>
      </div>
    </header>
  );
}
