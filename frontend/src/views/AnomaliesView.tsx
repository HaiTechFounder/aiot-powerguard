import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { AnomalyList } from "../components/AnomalyList";
import { ErrorState, LoadingState } from "../components/states";
import { isModelReady, useHealth } from "../hooks/useHealth";
import { useAnomalies } from "../hooks/useAnomalies";

export function AnomaliesView(): ReactNode {
  const { deviceId = "" } = useParams<{ deviceId: string }>();
  const anomalies = useAnomalies(deviceId);
  const health = useHealth();
  const modelReady = isModelReady(health.data);

  return (
    <section>
      <div className="page-head">
        <h1 className="page-title">Anomalies — {deviceId}</h1>
        <Link to={`/devices/${encodeURIComponent(deviceId)}`} className="button">
          Back to device
        </Link>
      </div>

      {anomalies.loading && !anomalies.data ? (
        <LoadingState label="Loading anomalies…" />
      ) : anomalies.error && !anomalies.data ? (
        <ErrorState error={anomalies.error} onRetry={anomalies.reload} />
      ) : (
        <>
          <AnomalyList anomalies={anomalies.data?.items ?? []} modelReady={modelReady} />
          {anomalies.data?.next_before_id ? (
            <p className="page-note">
              Older anomalies exist beyond this page; paging further is not part of this phase.
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
