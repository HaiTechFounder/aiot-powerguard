import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { DeviceDto } from "../api/contract";
import { StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/states";
import { formatAge, formatTimestamp } from "../format/time";
import { formatMetric } from "../format/units";
import { useDevices } from "../hooks/useDevices";

function DeviceRow({ device }: { device: DeviceDto }): ReactNode {
  const latest = device.latest ?? null;
  return (
    <li className="device-card">
      <div className="device-card__head">
        <Link to={`/devices/${encodeURIComponent(device.id)}`} className="device-card__id">
          {device.id}
        </Link>
        <StatusBadge status={device.status} />
      </div>
      <dl className="device-card__meta">
        <div>
          <dt>Firmware</dt>
          <dd>{device.firmware_version}</dd>
        </div>
        <div>
          <dt>Last seen</dt>
          <dd title={formatTimestamp(device.last_seen_at)}>{formatAge(device.last_seen_at)}</dd>
        </div>
      </dl>
      {latest ? (
        <dl className="device-card__latest">
          <div className="device-card__reading">
            <dt className="device-card__reading-label">Voltage</dt>
            <dd>{formatMetric(latest.voltage_v, "voltage")}</dd>
          </div>
          <div className="device-card__reading">
            <dt className="device-card__reading-label">Current</dt>
            <dd>{formatMetric(latest.current_a, "current")}</dd>
          </div>
          <div className="device-card__reading">
            <dt className="device-card__reading-label">Power</dt>
            <dd>{formatMetric(latest.power_w, "power")}</dd>
          </div>
        </dl>
      ) : (
        <p className="device-card__latest device-card__latest--empty">No readings yet</p>
      )}
    </li>
  );
}

export function DevicesView(): ReactNode {
  const devices = useDevices();

  if (devices.loading && !devices.data) return <LoadingState label="Loading devices…" />;
  if (devices.error && !devices.data) {
    return <ErrorState error={devices.error} onRetry={devices.reload} />;
  }

  const items = devices.data?.items ?? [];
  if (items.length === 0) {
    return (
      <EmptyState
        title="No devices have reported yet"
        hint="Start the backend and a device — or run the synthetic publisher — and they will appear here."
      />
    );
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2 className="page-title">Overview</h2>
          <p className="page-subtitle">
            {items.length} registered {items.length === 1 ? "device" : "devices"} · select one to
            open its dashboard
          </p>
        </div>
      </div>
      <ul className="device-list">
        {items.map((device) => (
          <DeviceRow key={device.id} device={device} />
        ))}
      </ul>
    </div>
  );
}
