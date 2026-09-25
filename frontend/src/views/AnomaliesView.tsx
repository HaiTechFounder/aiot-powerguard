import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { AnomalyList } from "../components/AnomalyList";
import { AlertIcon, ChipIcon } from "../components/icons";
import { ErrorState, LoadingState } from "../components/states";
import { isModelReady, useHealth } from "../hooks/useHealth";
import { useAnomalies } from "../hooks/useAnomalies";

export function AnomaliesView(): ReactNode {
  const { deviceId = "" } = useParams<{ deviceId: string }>();
  const anomalies = useAnomalies(deviceId);
  const health = useHealth();
  const modelReady = isModelReady(health.data);

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2 className="page-title">Anomaly history</h2>
          <p className="page-subtitle">{deviceId}</p>
        </div>
        <Link to={`/devices/${encodeURIComponent(deviceId)}`} className="button button--ghost">
          <ChipIcon size={16} />
          Back to device
        </Link>
      </div>

      <section className="card" aria-labelledby="anomaly-history-title">
        <div className="card__head">
          <div className="card__heading">
            <span className="card__icon">
              <AlertIcon size={18} />
            </span>
            <h2 className="section-title" id="anomaly-history-title">
              Recorded verdicts
            </h2>
          </div>
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
    </div>
  );
}
