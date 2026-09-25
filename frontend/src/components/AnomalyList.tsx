import type { ReactNode } from "react";

import type { AnomalyEntry } from "../api/contract";
import { formatTimestamp } from "../format/time";
import { formatMetric, formatScore } from "../format/units";
import { EmptyState } from "./states";

/**
 * Recorded anomalies, and — separately — whether anything is still looking.
 *
 * These are two different facts and the UI keeps them apart. Verdicts already
 * stored by the backend stay visible whatever the detector is doing now;
 * hiding them because the model is unloaded would lose real history. And an
 * empty list under an unloaded model means "not assessed", never "clean": that
 * claim needs a detector that actually ran.
 *
 * A verdict that arrived over the socket carries no measurements — the WS
 * envelope does not send them — so those cells show an em dash rather than a
 * fabricated reading.
 */
export function AnomalyList({
  anomalies,
  modelReady,
}: {
  anomalies: readonly AnomalyEntry[];
  modelReady: boolean;
}): ReactNode {
  return (
    <div className="anomaly-area">
      <p className="anomaly-area__status" data-testid="detection-status">
        Detection status: <strong>{modelReady ? "running" : "unavailable"}</strong>
      </p>

      {modelReady ? null : (
        <div className="state state--muted" data-testid="detection-unavailable">
          <p className="state__title">Anomaly detection unavailable</p>
          <p className="state__hint">
            No model is loaded, so no new reading is being assessed. This is not the same as “no
            anomalies”. Verdicts recorded earlier are still listed below.
          </p>
        </div>
      )}

      <h3 className="anomaly-area__title">Recorded anomaly history</h3>

      {anomalies.length === 0 ? (
        modelReady ? (
          <EmptyState
            title="No anomalies recorded"
            hint="No anomaly verdicts are stored for this device."
          />
        ) : (
          <EmptyState
            title="No recorded anomaly entries"
            hint="Nothing has been assessed while the detector is unavailable, so this is a count of stored verdicts — not a verdict of its own."
          />
        )
      ) : (
        <div className="table-wrap">
        <table className="table" data-testid="anomaly-table">
          <thead>
            <tr>
              <th scope="col">Detected</th>
              <th scope="col">Method</th>
              <th scope="col">Score</th>
              <th scope="col">Reasons</th>
              <th scope="col">Voltage</th>
              <th scope="col">Current</th>
              <th scope="col">Power</th>
            </tr>
          </thead>
          <tbody>
            {anomalies.map((anomaly) => (
              <tr key={anomaly.id}>
                <td>{formatTimestamp(anomaly.detected_at)}</td>
                <td>{anomaly.method}</td>
                <td>{formatScore(anomaly.score)}</td>
                <td>{anomaly.reasons.join(", ") || "—"}</td>
                <td>{formatMetric(anomaly.voltage_v, "voltage")}</td>
                <td>{formatMetric(anomaly.current_a, "current")}</td>
                <td>{formatMetric(anomaly.power_w, "power")}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}
