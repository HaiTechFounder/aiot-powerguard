import type { ReactNode } from "react";

import type { DeviceDto, DeviceStatus, HealthDto, TelemetryDto } from "../api/contract";
import type { ApiError } from "../api/errors";
import { formatAge, formatTimestamp } from "../format/time";
import { ActivityIcon, ChipIcon, GridIcon } from "./icons";
import { StatusBadge } from "./StatusBadge";

/**
 * Device, sensor and system properties — only the ones the API actually sends.
 *
 * `DeviceDto` carries an id, a firmware version, a status and two timestamps;
 * `TelemetryDto` adds the boot id, sequence number, sensor status and the two
 * clocks behind a reading; `HealthDto` carries the four subsystem states and a
 * version. There is no signal strength, address or uptime in any of the three
 * contracts, so none is shown — an invented field on a monitoring console is
 * worse than a missing one.
 *
 * `sampled_at` is null until the firmware has a time source, and says so
 * rather than borrowing `received_at`.
 */
function Row({ term, children }: { term: string; children: ReactNode }): ReactNode {
  return (
    <div className="props__row">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function RailCard({
  title,
  icon,
  testId,
  action,
  children,
}: {
  title: string;
  icon: ReactNode;
  testId: string;
  action?: ReactNode;
  children: ReactNode;
}): ReactNode {
  const id = `${testId}-title`;
  return (
    <section className="card" aria-labelledby={id} data-testid={testId}>
      <div className="card__head">
        <div className="card__heading">
          <span className="card__icon">{icon}</span>
          <h2 className="section-title" id={id}>
            {title}
          </h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function DeviceInfoPanel({
  deviceId,
  device,
  latest,
  liveStatus,
  loading,
  unavailable,
  health,
  healthError,
}: {
  deviceId: string;
  device: DeviceDto | null;
  latest: TelemetryDto | null;
  liveStatus: DeviceStatus | null;
  loading: boolean;
  unavailable: boolean;
  health: HealthDto | null;
  healthError: ApiError | null;
}): ReactNode {
  // A live `status` frame is newer than the polled list, so it wins when present.
  const status = liveStatus ?? device?.status ?? null;
  const pending = loading ? "Loading…" : "—";

  return (
    <>
      <RailCard
        title="Device Information"
        icon={<ChipIcon size={18} />}
        testId="device-info"
        action={status ? <StatusBadge status={status} /> : null}
      >
        <dl className="props">
          <Row term="Device ID">{deviceId}</Row>
          <Row term="Firmware version">{device ? device.firmware_version : pending}</Row>
          <Row term="First seen">
            {device ? formatTimestamp(device.first_seen_at) : pending}
          </Row>
          <Row term="Last seen">
            {device ? (
              <span title={formatTimestamp(device.last_seen_at)}>
                {formatAge(device.last_seen_at)}
              </span>
            ) : (
              pending
            )}
          </Row>
        </dl>
        {unavailable ? (
          <p className="page-note" data-testid="device-props-unavailable">
            The device registry could not be read, so these properties are not current.
          </p>
        ) : null}
      </RailCard>

      <RailCard title="Latest Reading" icon={<ActivityIcon size={18} />} testId="sensor-info">
        {latest ? (
          <dl className="props">
            <Row term="Sensor status">{latest.sensor_status}</Row>
            <Row term="Received at (server)">{formatTimestamp(latest.received_at)}</Row>
            <Row term="Sampled at (device)">
              {latest.sampled_at ? formatTimestamp(latest.sampled_at) : "not reported"}
            </Row>
            <Row term="Sequence">{latest.seq}</Row>
            <Row term="Boot ID">{latest.boot_id}</Row>
            <Row term="Row ID">{latest.id}</Row>
          </dl>
        ) : (
          <p className="page-note">No reading has been loaded for this device yet.</p>
        )}
      </RailCard>

      <RailCard title="System Information" icon={<GridIcon size={18} />} testId="system-info">
        {healthError ? (
          <p className="page-note">
            The health endpoint could not be reached, so these states are unknown.
          </p>
        ) : health ? (
          <dl className="props">
            <Row term="Backend">{health.status}</Row>
            <Row term="Database">{health.database}</Row>
            <Row term="MQTT broker">{health.mqtt}</Row>
            <Row term="Anomaly model">{health.model}</Row>
            <Row term="API version">{health.version}</Row>
          </dl>
        ) : (
          <p className="page-note">Reading system health…</p>
        )}
      </RailCard>
    </>
  );
}
