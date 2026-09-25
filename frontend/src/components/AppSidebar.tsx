/**
 * The navy rail: brand, the three places this dashboard can go, and health.
 *
 * Every entry maps onto a route that exists — `/`, `/devices/:id` and
 * `/devices/:id/anomalies` — and nothing else is listed. Two of the three need
 * a device to be meaningful, so when none is selected they are rendered as
 * inert text with the reason stated, rather than as links to a URL that would
 * resolve to the not-found page.
 *
 * The status card at the bottom shows what `/health` actually returned. A
 * disconnected broker and an absent model are real conditions here, so they
 * are drawn as amber rather than quietly rounded up to green.
 */

import type { ReactNode } from "react";
import { NavLink, useLocation, useParams } from "react-router-dom";

import type { HealthDto } from "../api/contract";
import type { ApiError } from "../api/errors";
import { AlertIcon, ChipIcon, GridIcon, LogoMark } from "./icons";

function Entry({
  to,
  label,
  icon,
  active,
}: {
  to: string | null;
  label: string;
  icon: ReactNode;
  active: boolean;
}): ReactNode {
  if (to === null) {
    return (
      <span className="sidebar__link sidebar__link--disabled" aria-disabled="true">
        {icon}
        {label}
      </span>
    );
  }
  return (
    <NavLink
      to={to}
      end
      className={`sidebar__link${active ? " is-active" : ""}`}
      aria-current={active ? "page" : undefined}
    >
      {icon}
      {label}
    </NavLink>
  );
}

function StatusRow({
  label,
  value,
  ok,
}: {
  label: string;
  value: string;
  ok: boolean;
}): ReactNode {
  return (
    <p className="sidebar__status-row">
      <span className={`dot ${ok ? "dot--ok" : "dot--warn"}`} aria-hidden="true" />
      <span>{label}</span>
      <span>{value}</span>
    </p>
  );
}

export function AppSidebar({
  health,
  healthError,
}: {
  health: HealthDto | null;
  healthError: ApiError | null;
}): ReactNode {
  const { deviceId } = useParams<{ deviceId: string }>();
  const { pathname } = useLocation();
  const encoded = deviceId ? encodeURIComponent(deviceId) : null;

  return (
    <nav className="sidebar" aria-label="Sections" data-testid="sidebar">
      <NavLink to="/" className="sidebar__brand">
        <LogoMark size={38} />
        <span className="sidebar__brand-text">
          <span className="sidebar__brand-name">PowerGuard</span>
          <span className="sidebar__brand-sub">AIoT energy monitoring</span>
        </span>
      </NavLink>

      <div>
        <p className="sidebar__section-label">Monitoring</p>
        <div className="sidebar__nav">
          <Entry to="/" label="Overview" icon={<GridIcon />} active={pathname === "/"} />
          <Entry
            to={encoded ? `/devices/${encoded}` : null}
            label="Devices"
            icon={<ChipIcon />}
            active={Boolean(encoded) && !pathname.endsWith("/anomalies")}
          />
          <Entry
            to={encoded ? `/devices/${encoded}/anomalies` : null}
            label="Anomaly History"
            icon={<AlertIcon />}
            active={pathname.endsWith("/anomalies")}
          />
        </div>
        {encoded ? null : (
          <p className="sidebar__hint">
            Select a device from the overview to open its dashboard and anomaly history.
          </p>
        )}
      </div>

      <div className="sidebar__status" data-testid="sidebar-status">
        <p className="sidebar__status-title">System status</p>
        {healthError ? (
          <StatusRow label="Backend" value="unreachable" ok={false} />
        ) : health ? (
          <>
            <StatusRow label="Backend" value={health.status} ok={health.status === "ok"} />
            <StatusRow
              label="Database"
              value={health.database}
              ok={health.database === "ready"}
            />
            <StatusRow label="MQTT" value={health.mqtt} ok={health.mqtt === "connected"} />
            <StatusRow label="Model" value={health.model} ok={health.model === "ready"} />
          </>
        ) : (
          <p className="sidebar__status-row">
            <span className="dot dot--idle" aria-hidden="true" />
            <span>Checking…</span>
          </p>
        )}
      </div>
    </nav>
  );
}
