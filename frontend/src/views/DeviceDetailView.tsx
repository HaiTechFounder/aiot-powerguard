import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { AnomalyEntry, TelemetryDto } from "../api/contract";
import { AnomalyList } from "../components/AnomalyList";
import { ConnectionBanner } from "../components/ConnectionBanner";
import { DeviceInfoPanel } from "../components/DeviceInfoPanel";
import { LiveBadge } from "../components/LiveBadge";
import { LiveTelemetryChart } from "../components/LiveTelemetryChart";
import { MetricCard } from "../components/MetricCard";
import { MetricChart } from "../components/MetricChart";
import { StatusBadge } from "../components/StatusBadge";
import { AlertIcon } from "../components/icons";
import { EmptyState, ErrorState, LoadingState } from "../components/states";
import { formatTimestamp } from "../format/time";
import { useAnomalies } from "../hooks/useAnomalies";
import { useDevices } from "../hooks/useDevices";
import { isModelReady, useHealth } from "../hooks/useHealth";
import { useLiveState } from "../hooks/useLiveState";
import { useDeviceStream } from "../realtime/useDeviceStream";

/**
 * Stored verdicts first, then anything the socket has added since.
 *
 * The list is billed as recorded history, so it has to be the recorded
 * history: REST is the source, and a live frame is only ever an addition to
 * it. Ids are the join — a verdict that arrived on the socket and again in the
 * page is one verdict — and the REST row wins the tie because it carries the
 * measurements the frame does not.
 */
function mergeAnomalies(
  stored: readonly AnomalyEntry[],
  live: readonly AnomalyEntry[],
): AnomalyEntry[] {
  const byId = new Map<number, AnomalyEntry>();
  for (const entry of live) byId.set(entry.id, entry);
  for (const entry of stored) byId.set(entry.id, entry);
  return [...byId.values()].sort((a, b) =>
    a.detected_at === b.detected_at ? b.id - a.id : a.detected_at < b.detected_at ? 1 : -1,
  );
}

/**
 * The window sizes the hero chart offers, in readings rather than in hours.
 *
 * A device's publish interval is not in any contract, so "last hour" would be
 * a guess dressed up as a filter. Counting readings is something this view can
 * actually prove, and an option is only offered once there is more data than
 * it would show — a "Last 200" button over 12 rows filters nothing.
 */
const WINDOWS = [60, 200] as const;

function RangeControl({
  options,
  value,
  total,
  onChange,
}: {
  options: readonly number[];
  value: number | null;
  total: number;
  onChange: (next: number | null) => void;
}): ReactNode {
  if (options.length === 0) return null;
  return (
    <div className="range" role="group" aria-label="Readings shown">
      {options.map((size) => (
        <button
          key={size}
          type="button"
          className={`range__button${value === size ? " is-active" : ""}`}
          aria-pressed={value === size}
          onClick={() => onChange(size)}
        >
          Last {size}
        </button>
      ))}
      <button
        type="button"
        className={`range__button${value === null ? " is-active" : ""}`}
        aria-pressed={value === null}
        onClick={() => onChange(null)}
      >
        All {total}
      </button>
    </div>
  );
}

export function DeviceDetailView(): ReactNode {
  const { deviceId = "" } = useParams<{ deviceId: string }>();
  const stream = useDeviceStream(deviceId);
  const history = useAnomalies(deviceId);
  const health = useHealth();
  // The registry read is what supplies firmware and the first/last-seen pair;
  // the socket never sends them.
  const devices = useDevices();
  const modelReady = isModelReady(health.data);
  const [windowSize, setWindowSize] = useState<number | null>(null);

  const storedAnomalies = history.data?.items;
  const liveAnomalies = stream.anomalies;
  const anomalies = useMemo(
    () => mergeAnomalies(storedAnomalies ?? [], liveAnomalies),
    [storedAnomalies, liveAnomalies],
  );

  const series = stream.series;
  const latest = series.length ? (series[series.length - 1] ?? null) : null;
  const device = devices.data?.items.find((item) => item.id === deviceId) ?? null;

  // A live `status` frame is newer than the polled registry, so it wins.
  const deviceStatus = stream.status ?? device?.status ?? null;
  // The badge is a claim about the device, and it has to keep being re-checked
  // as time passes: `useLiveState` ticks so freshness can expire with no
  // further frames, and clears its timer on unmount.
  const live = useLiveState({
    deviceId,
    health: health.data,
    healthFailed: Boolean(health.error),
    connection: stream.connection,
    deviceStatus,
    latest,
  });

  // Only offer a window that would actually drop something.
  const options = WINDOWS.filter((size) => series.length > size);
  const shown: readonly TelemetryDto[] =
    windowSize !== null && series.length > windowSize ? series.slice(-windowSize) : series;

  const sparks = useMemo(() => {
    const tail = series.slice(-40);
    return {
      voltage: tail.map((row) => row.voltage_v),
      current: tail.map((row) => row.current_a),
      power: tail.map((row) => row.power_w),
      energy: tail.map((row) => row.energy_wh),
    };
  }, [series]);

  const charts = useMemo(
    () =>
      stream.seed === "loading" ? (
      <LoadingState label="Loading history…" />
    ) : series.length === 0 ? (
      <EmptyState
        title="No readings for this device yet"
        hint="It has registered but has not published telemetry."
      />
    ) : (
      <>
        <LiveTelemetryChart
          series={shown}
          isLive={live.isLive}
          controls={
            <RangeControl
              options={options}
              value={windowSize}
              total={series.length}
              onChange={setWindowSize}
            />
          }
        />
        <div className="chart-grid">
          <MetricChart series={series} metric="voltage" label="Voltage" />
          <MetricChart series={series} metric="current" label="Current" />
          <MetricChart series={series} metric="power" label="Power" />
        </div>
      </>
      ),
    [stream.seed, series, shown, options, windowSize, live.isLive],
  );

  return (
    <div className="stack">
      <div className="page-head">
        <div className="page-head__id">
          <h2 className="page-title">{deviceId}</h2>
          {deviceStatus ? <StatusBadge status={deviceStatus} /> : null}
        </div>
        <Link
          to={`/devices/${encodeURIComponent(deviceId)}/anomalies`}
          className="button button--ghost"
        >
          <AlertIcon size={16} />
          Anomaly history
        </Link>
      </div>

      {/* The device verdict and the transport state, in that order and clearly
          apart: one is a claim about the device, the other about this socket. */}
      <LiveBadge state={live} latest={latest} />

      <ConnectionBanner
        connection={stream.connection}
        catchUp={stream.catchUp}
        historyIncomplete={stream.historyIncomplete}
        onRetry={stream.retryNow}
      />

      {/* A failed seed does not hide the live values the socket may still deliver. */}
      {stream.seed === "failed" && stream.error ? <ErrorState error={stream.error} /> : null}

      <div className="dashboard">
        <div className="dashboard__main">
          <p className="metric-row__caption" data-testid="metric-provenance">
            {live.isLive ? (
              <strong>Live measurements</strong>
            ) : (
              <>
                <strong>Last known readings</strong>
                <span>
                  not live measurements — {live.label.toLowerCase()}
                  {latest ? ` · received ${formatTimestamp(latest.received_at)}` : ""}
                </span>
              </>
            )}
          </p>
          <div className="metric-row">
            <MetricCard
              label="Voltage"
              value={latest?.voltage_v}
              metric="voltage"
              history={sparks.voltage}
            />
            <MetricCard
              label="Current"
              value={latest?.current_a}
              metric="current"
              history={sparks.current}
            />
            <MetricCard
              label="Power"
              value={latest?.power_w}
              metric="power"
              history={sparks.power}
            />
            <MetricCard
              label="Energy"
              value={latest?.energy_wh}
              metric="energy"
              history={sparks.energy}
            />
          </div>

          {latest ? (
            <p className="page-note">
              Latest reading received {formatTimestamp(latest.received_at)} · sequence{" "}
              {latest.seq} · boot {latest.boot_id}
            </p>
          ) : null}

          {charts}
        </div>

        <aside className="dashboard__rail" aria-label="Device information">
          <DeviceInfoPanel
            deviceId={deviceId}
            device={device}
            latest={latest}
            liveStatus={stream.status}
            loading={devices.loading && !devices.data}
            unavailable={Boolean(devices.error) && !device}
            health={health.data}
            healthError={health.error}
          />
        </aside>
      </div>

      <section className="card" aria-labelledby="recent-anomalies-title">
        <div className="card__head">
          <div className="card__heading">
            <span className="card__icon">
              <AlertIcon size={18} />
            </span>
            <h2 className="section-title" id="recent-anomalies-title">
              Recent anomalies
            </h2>
          </div>
        </div>
        {/* An unread history is not an empty one: never call it "none stored"
            until the stored verdicts have actually been fetched. */}
        {history.loading && !history.data ? (
          <LoadingState label="Loading anomaly history…" />
        ) : history.error && !history.data ? (
          <ErrorState error={history.error} onRetry={history.reload} />
        ) : (
          <AnomalyList anomalies={anomalies} modelReady={modelReady} />
        )}
      </section>
    </div>
  );
}
