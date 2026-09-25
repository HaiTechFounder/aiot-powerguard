import type { ReactNode } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { TelemetryDto } from "../api/contract";
import { formatClock } from "../format/time";
import { UNITS, formatValue } from "../format/units";
import { METRIC_STROKE, PALETTE } from "../theme";
import { ActivityIcon } from "./icons";

/**
 * The headline chart: voltage and current on one time base.
 *
 * Volts and amps are different quantities, so they get different axes —
 * azure on the left for V, cyan on the right for A — each labelled with its
 * own unit. Sharing one axis would make the two lines look comparable when
 * their scales have nothing to do with each other.
 *
 * Both axes auto-range over the data actually loaded rather than starting at
 * zero: a mains reading that varies by a volt is invisible on a 0–250 scale,
 * and inventing headroom is its own kind of untruth.
 *
 * `received_at` is the x axis for the same reason as everywhere else: it is
 * the authoritative order (ADR-006), while `sampled_at` is diagnostic and null
 * until the firmware has a time source.
 */
export function LiveTelemetryChart({
  series,
  isLive,
  controls,
}: {
  series: readonly TelemetryDto[];
  /**
   * The page's live verdict (`evaluateLiveState`). The same series is
   * recorded history whenever the device, broker, socket or freshness check
   * fails, and the heading must say so rather than call it live.
   */
  isLive: boolean;
  controls?: ReactNode;
}): ReactNode {
  const data = series.map((row) => ({
    at: row.received_at,
    voltage: row.voltage_v,
    current: row.current_a,
    flagged: Boolean(row.anomaly),
  }));
  const flagged = data.filter((point) => point.flagged);

  return (
    <figure className="chart card">
      <figcaption className="chart__caption">
        <div className="card__head">
          <div className="card__heading">
            <span className="card__icon">
              <ActivityIcon size={18} />
            </span>
            <div>
              <h2 className="chart__title">{isLive ? "Live Telemetry" : "Telemetry history"}</h2>
              <p className="chart__sub">
                Voltage and current · {series.length}{" "}
                {series.length === 1 ? "reading" : "readings"} shown
              </p>
            </div>
          </div>
          {controls}
        </div>
      </figcaption>

      <div className="chart__canvas" data-testid="chart-live">
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
            <defs>
              <linearGradient id="pg-fill-voltage" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={METRIC_STROKE.voltage} stopOpacity={0.22} />
                <stop offset="100%" stopColor={METRIC_STROKE.voltage} stopOpacity={0.01} />
              </linearGradient>
              <linearGradient id="pg-fill-current" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={METRIC_STROKE.current} stopOpacity={0.18} />
                <stop offset="100%" stopColor={METRIC_STROKE.current} stopOpacity={0.01} />
              </linearGradient>
            </defs>

            <CartesianGrid stroke={PALETTE.grid} vertical={false} />
            <XAxis
              dataKey="at"
              tickFormatter={formatClock}
              minTickGap={64}
              stroke={PALETTE.grid}
              tick={{ fontSize: 11, fill: PALETTE.axis }}
              tickLine={false}
            />
            <YAxis
              yAxisId="voltage"
              width={78}
              stroke={PALETTE.grid}
              tick={{ fontSize: 11, fill: METRIC_STROKE.voltage }}
              tickLine={false}
              tickFormatter={(value: number) => formatValue(value, "voltage")}
              domain={["auto", "auto"]}
              label={{
                value: `Voltage (${UNITS.voltage})`,
                angle: -90,
                position: "insideLeft",
                style: { fontSize: 11, fill: METRIC_STROKE.voltage, textAnchor: "middle" },
              }}
            />
            <YAxis
              yAxisId="current"
              orientation="right"
              width={78}
              stroke={PALETTE.grid}
              tick={{ fontSize: 11, fill: METRIC_STROKE.current }}
              tickLine={false}
              tickFormatter={(value: number) => formatValue(value, "current")}
              domain={["auto", "auto"]}
              label={{
                value: `Current (${UNITS.current})`,
                angle: 90,
                position: "insideRight",
                style: { fontSize: 11, fill: METRIC_STROKE.current, textAnchor: "middle" },
              }}
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
              formatter={(value: number, name: string) =>
                name === "Voltage"
                  ? [`${formatValue(value, "voltage")} ${UNITS.voltage}`, name]
                  : [`${formatValue(value, "current")} ${UNITS.current}`, name]
              }
            />

            <Area
              yAxisId="voltage"
              name="Voltage"
              type="monotone"
              dataKey="voltage"
              stroke={METRIC_STROKE.voltage}
              strokeWidth={2.4}
              fill="url(#pg-fill-voltage)"
              isAnimationActive={false}
              activeDot={{ r: 4 }}
            />
            <Area
              yAxisId="current"
              name="Current"
              type="monotone"
              dataKey="current"
              stroke={METRIC_STROKE.current}
              strokeWidth={2.4}
              fill="url(#pg-fill-current)"
              isAnimationActive={false}
              activeDot={{ r: 4 }}
            />
            {/* A zero-width line keeps the legend colours honest if an Area
                is ever swapped out; Recharts needs a series per axis. */}
            <Line yAxisId="voltage" dataKey="voltage" stroke="none" dot={false} legendType="none" />

            {flagged.map((point) => (
              <ReferenceDot
                key={point.at}
                yAxisId="voltage"
                x={point.at}
                y={point.voltage}
                r={4.5}
                fill={PALETTE.error}
                stroke={PALETTE.surface}
                strokeWidth={1.5}
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <p className="chart__legend">
        <span className="chart__legend-item">
          <span
            className="chart__swatch"
            style={{ background: METRIC_STROKE.voltage }}
            aria-hidden="true"
          />
          Voltage — left axis ({UNITS.voltage})
        </span>
        <span className="chart__legend-item">
          <span
            className="chart__swatch"
            style={{ background: METRIC_STROKE.current }}
            aria-hidden="true"
          />
          Current — right axis ({UNITS.current})
        </span>
        {flagged.length > 0 ? (
          <span className="chart__legend-item">
            <span
              className="chart__swatch"
              style={{ background: PALETTE.error, borderRadius: "50%" }}
              aria-hidden="true"
            />
            Reading with a recorded anomaly
          </span>
        ) : null}
      </p>
    </figure>
  );
}
