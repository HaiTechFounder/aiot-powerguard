import type { ReactNode } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { TelemetryDto } from "../api/contract";
import { formatClock } from "../format/time";
import type { Metric } from "../format/units";
import { UNITS, formatValue } from "../format/units";

const FIELD: Record<Metric, keyof TelemetryDto> = {
  voltage: "voltage_v",
  current: "current_a",
  power: "power_w",
  energy: "energy_wh",
};

/**
 * One metric over time, with `received_at` on the x axis.
 *
 * `received_at` is the authoritative order (ADR-006); `sampled_at` is
 * diagnostic and is null until the firmware has a time source, so it would
 * make a useless axis.
 */
export function MetricChart({
  series,
  metric,
  label,
}: {
  series: readonly TelemetryDto[];
  metric: Metric;
  label: string;
}): ReactNode {
  const field = FIELD[metric];
  const data = series.map((row) => ({
    at: row.received_at,
    value: row[field] as number,
    flagged: Boolean(row.anomaly),
  }));
  const flagged = data.filter((point) => point.flagged);

  return (
    <figure className="chart">
      <figcaption className="chart__caption">
        {label} <span className="chart__unit">({UNITS[metric]})</span>
      </figcaption>
      <div className="chart__canvas" data-testid={`chart-${metric}`}>
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#2a3342" />
            <XAxis dataKey="at" tickFormatter={formatClock} minTickGap={48} stroke="#8b96a8" />
            <YAxis
              width={64}
              stroke="#8b96a8"
              tickFormatter={(value: number) => formatValue(value, metric)}
              domain={["auto", "auto"]}
            />
            <Tooltip
              labelFormatter={(value: string) => formatClock(value)}
              formatter={(value: number) => [`${formatValue(value, metric)} ${UNITS[metric]}`, label]}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke="#4da3ff"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            {flagged.map((point) => (
              <ReferenceDot
                key={point.at}
                x={point.at}
                y={point.value}
                r={4}
                fill="#ff6b6b"
                stroke="none"
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
