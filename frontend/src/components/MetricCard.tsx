import type { ReactNode } from "react";

import type { Metric } from "../format/units";
import { formatMetric } from "../format/units";

export function MetricCard({
  label,
  value,
  metric,
}: {
  label: string;
  value: number | null | undefined;
  metric: Metric;
}): ReactNode {
  return (
    <div className="metric-card">
      <span className="metric-card__label">{label}</span>
      <span className="metric-card__value" data-testid={`metric-${metric}`}>
        {formatMetric(value, metric)}
      </span>
    </div>
  );
}
