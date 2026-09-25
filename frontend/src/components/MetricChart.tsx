import type { ReactNode } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
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
import { METRIC_STROKE, PALETTE } from "../theme";
import { BatteryIcon, BoltIcon, PowerIcon, WaveIcon } from "./icons";

const FIELD: Record<Metric, keyof TelemetryDto> = {
  voltage: "voltage_v",
  current: "current_a",
  power: "power_w",
  energy: "energy_wh",
};

const ICON: Record<Metric, ReactNode> = {
  voltage: <BoltIcon size={17} />,
  current: <WaveIcon size={17} />,
  power: <PowerIcon size={17} />,
  energy: <BatteryIcon size={17} />,
};

/**
 * One metric over time, with `received_at` on the x axis.
 *
 * `received_at` is the authoritative order (ADR-006); `sampled_at` is
 * diagnostic and is null until the firmware has a time source, so it would
 * make a useless axis. The y domain follows the data rather than starting at
 * zero — these are trends, and a fixed floor would flatten every one of them.
 */
export function MetricChart({
  series,
  metric,
  label,
  height = 168,
}: {
  series: readonly TelemetryDto[];
  metric: Metric;
  label: string;
  height?: number;
}): ReactNode {
  const field = FIELD[metric];
  const stroke = METRIC_STROKE[metric];
  const gradientId = `pg-trend-${metric}`;
  const data = series.map((row) => ({
    at: row.received_at,
    value: row[field] as number,
    flagged: Boolean(row.anomaly),
  }));
  const flagged = data.filter((point) => point.flagged);

  return (
    <figure className="chart card">
      <figcaption className="chart__caption">
        <div className="card__head">
          <div className="card__heading">
            <span
              className="card__icon"
              style={{ background: `${stroke}1F`, color: stroke }}
            >
              {ICON[metric]}
            </span>
            <div>
              <h3 className="chart__title">
                {label} Trend <span className="chart__unit">({UNITS[metric]})</span>
              </h3>
              <p className="chart__sub">
                {series.length} {series.length === 1 ? "reading" : "readings"}
              </p>
            </div>
          </div>
        </div>
      </figcaption>

      <div className="chart__canvas" data-testid={`chart-${metric}`}>
        <ResponsiveContainer width="100%" height={height}>
          <AreaChart data={data} margin={{ top: 6, right: 8, bottom: 2, left: 0 }}>
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity={0.2} />
                <stop offset="100%" stopColor={stroke} stopOpacity={0.01} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={PALETTE.grid} vertical={false} />
            <XAxis
              dataKey="at"
              tickFormatter={formatClock}
              minTickGap={44}
              stroke={PALETTE.grid}
              tick={{ fontSize: 10.5, fill: PALETTE.axis }}
              tickLine={false}
            />
            <YAxis
              width={58}
              stroke={PALETTE.grid}
              tick={{ fontSize: 10.5, fill: PALETTE.axis }}
              tickLine={false}
              tickFormatter={(value: number) => formatValue(value, metric)}
              domain={["auto", "auto"]}
            />
            <Tooltip
              labelFormatter={(value: string) => formatClock(value)}
              contentStyle={{
                background: PALETTE.surface,
                border: `1px solid ${PALETTE.grid}`,
                borderRadius: 12,
                fontSize: 12.5,
                color: PALETTE.text,
                boxShadow: "0 8px 24px rgba(16, 36, 58, 0.12)",
              }}
              formatter={(value: number) => [
                `${formatValue(value, metric)} ${UNITS[metric]}`,
                label,
              ]}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke={stroke}
              strokeWidth={2.2}
              fill={`url(#${gradientId})`}
              isAnimationActive={false}
              activeDot={{ r: 3.5 }}
            />
            {flagged.map((point) => (
              <ReferenceDot
                key={point.at}
                x={point.at}
                y={point.value}
                r={4}
                fill={PALETTE.error}
                stroke={PALETTE.surface}
                strokeWidth={1.5}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
