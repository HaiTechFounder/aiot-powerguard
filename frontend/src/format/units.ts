/**
 * Units and decimals, fixed to what the device actually emits.
 *
 * Voltage, current and power arrive at three decimals and energy at six; the
 * dashboard shows exactly that precision. Rounding further would imply a
 * measurement the sensor never made.
 */

export const UNITS = {
  voltage: "V",
  current: "A",
  power: "W",
  energy: "Wh",
} as const;

export type Metric = keyof typeof UNITS;

const DECIMALS: Record<Metric, number> = {
  voltage: 3,
  current: 3,
  power: 3,
  energy: 6,
};

/** A number with its unit, or an em dash when there is nothing to show. */
export function formatMetric(value: number | null | undefined, metric: Metric): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toFixed(DECIMALS[metric])} ${UNITS[metric]}`;
}

/** The bare number, for a chart axis where the unit is in the label. */
export function formatValue(value: number | null | undefined, metric: Metric): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(DECIMALS[metric]);
}

/** An anomaly score is 0..1; two decimals is enough to compare them. */
export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined || !Number.isFinite(score)) return "—";
  return score.toFixed(2);
}
