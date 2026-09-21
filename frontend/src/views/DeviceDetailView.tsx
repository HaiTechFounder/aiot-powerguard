import type { ReactNode } from "react";
import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import type { AnomalyEntry } from "../api/contract";
import { AnomalyList } from "../components/AnomalyList";
import { ConnectionBanner } from "../components/ConnectionBanner";
import { MetricCard } from "../components/MetricCard";
import { MetricChart } from "../components/MetricChart";
import { StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/states";
import { formatTimestamp } from "../format/time";
import { useAnomalies } from "../hooks/useAnomalies";
import { isModelReady, useHealth } from "../hooks/useHealth";
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

export function DeviceDetailView(): ReactNode {
  const { deviceId = "" } = useParams<{ deviceId: string }>();
  const stream = useDeviceStream(deviceId);
  const history = useAnomalies(deviceId);
  const health = useHealth();
  const modelReady = isModelReady(health.data);

  const storedAnomalies = history.data?.items;
  const liveAnomalies = stream.anomalies;
  const anomalies = useMemo(
    () => mergeAnomalies(storedAnomalies ?? [], liveAnomalies),
    [storedAnomalies, liveAnomalies],
  );

  const latest = stream.series.length ? stream.series[stream.series.length - 1] : null;

  return (
    <section>
      <div className="page-head">
        <div>
          <h1 className="page-title">{deviceId}</h1>
          {stream.status ? <StatusBadge status={stream.status} /> : null}
        </div>
        <Link to={`/devices/${encodeURIComponent(deviceId)}/anomalies`} className="button">
          Anomaly history
        </Link>
      </div>

      <ConnectionBanner
        connection={stream.connection}
        catchUp={stream.catchUp}
        historyIncomplete={stream.historyIncomplete}
        onRetry={stream.retryNow}
      />

      {/* A failed seed does not hide the live values the socket may still deliver. */}
      {stream.seed === "failed" && stream.error ? (
        <ErrorState error={stream.error} />
      ) : null}

      <div className="metric-row">
        <MetricCard label="Voltage" value={latest?.voltage_v} metric="voltage" />
        <MetricCard label="Current" value={latest?.current_a} metric="current" />
        <MetricCard label="Power" value={latest?.power_w} metric="power" />
        <MetricCard label="Energy" value={latest?.energy_wh} metric="energy" />
      </div>

      {latest ? (
        <p className="page-note">
          Latest reading received {formatTimestamp(latest.received_at)} · sequence {latest.seq} ·
          boot {latest.boot_id}
        </p>
      ) : null}

      {stream.seed === "loading" ? (
        <LoadingState label="Loading history…" />
      ) : stream.series.length === 0 ? (
        <EmptyState
          title="No readings for this device yet"
          hint="It has registered but has not published telemetry."
        />
      ) : (
        <div className="chart-grid">
          <MetricChart series={stream.series} metric="voltage" label="Voltage" />
          <MetricChart series={stream.series} metric="current" label="Current" />
          <MetricChart series={stream.series} metric="power" label="Power" />
        </div>
      )}

      <h2 className="section-title">Recent anomalies</h2>
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
  );
}
