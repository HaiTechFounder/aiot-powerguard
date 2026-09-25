import type { ReactNode } from "react";

import type { Metric } from "../format/units";
import { UNITS, formatMetric } from "../format/units";
import { BatteryIcon, BoltIcon, PowerIcon, WaveIcon } from "./icons";
import { Sparkline } from "./Sparkline";

const ICON: Record<Metric, ReactNode> = {
  voltage: <BoltIcon size={19} />,
  current: <WaveIcon size={19} />,
  power: <PowerIcon size={19} />,
  energy: <BatteryIcon size={19} />,
};

/**
 * Ink for each card's own background, since a sparkline has to sit on it.
 *
 * The white power card needs an azure line; the three coloured cards need a
 * line that reads against their own fill rather than one borrowed from
 * another metric.
 */
const SPARK: Record<Metric, { stroke: string; fill: string }> = {
  voltage: { stroke: "#FFFFFF", fill: "rgba(255, 255, 255, 0.22)" },
  current: { stroke: "#0B3A52", fill: "rgba(11, 58, 82, 0.16)" },
  power: { stroke: "#007FFF", fill: "rgba(0, 127, 255, 0.12)" },
  energy: { stroke: "#FFFFFF", fill: "rgba(255, 255, 255, 0.22)" },
};

/**
 * One live value, on a card coloured by its own metric.
 *
 * The sparkline is drawn from the readings already loaded and from nothing
 * else — no trend arrow, no percentage. A delta nobody computed is a delta
 * nobody can trust, and the backend publishes none.
 */
export function MetricCard({
  label,
  value,
  metric,
  history = [],
}: {
  label: string;
  value: number | null | undefined;
  metric: Metric;
  history?: readonly number[];
}): ReactNode {
  const spark = SPARK[metric];
  const hasTrend = history.length >= 2;

  return (
    <div className={`metric-card metric-card--${metric}`}>
      <div className="metric-card__top">
        <span className="metric-card__label">
          {label} ({UNITS[metric]})
        </span>
        <span className="metric-card__icon">{ICON[metric]}</span>
      </div>

      <span className="metric-card__value" data-testid={`metric-${metric}`}>
        {formatMetric(value, metric)}
      </span>

      {hasTrend ? (
        <Sparkline
          values={history}
          stroke={spark.stroke}
          fill={spark.fill}
          label={`${label} across the last ${history.length} loaded readings`}
        />
      ) : (
        <p className="metric-card__foot metric-card__spark">
          {/* Fewer than two readings is not a trend, and is not drawn as one. */}
          Not enough readings for a trend
        </p>
      )}
    </div>
  );
}
