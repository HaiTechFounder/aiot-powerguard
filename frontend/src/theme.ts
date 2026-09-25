/**
 * The palette, in one place, for the parts of the UI that cannot use CSS.
 *
 * Recharts draws into SVG attributes rather than class names, so the chart
 * colours have to exist as values. They are the same tokens `styles.css`
 * declares, and each metric keeps its colour everywhere it appears — a line,
 * a swatch, a KPI card — so a reader never has to re-learn the mapping.
 */

import type { Metric } from "./format/units";

export const PALETTE = {
  navy: "#10243A",
  primary: "#007FFF",
  sky: "#66CCFF",
  action: "#1E90FF",
  mint: "#4ADE80",
  deepGreen: "#065F46",
  text: "#10243A",
  muted: "#5A6572",
  grid: "#E4EEF8",
  axis: "#8195AA",
  surface: "#FFFFFF",
  error: "#DC2626",
} as const;

/** Voltage azure, current cyan, power deep green, energy mint. */
export const METRIC_COLOR: Record<Metric, string> = {
  voltage: PALETTE.primary,
  current: PALETTE.sky,
  power: PALETTE.deepGreen,
  energy: PALETTE.mint,
};

/**
 * A stroke that stays legible on a white card.
 *
 * `--sky` is a light cyan: correct as a fill or a card background, too pale as
 * a hairline on white. Current therefore draws in a darkened cyan of the same
 * hue rather than in a colour borrowed from another metric.
 */
export const METRIC_STROKE: Record<Metric, string> = {
  ...METRIC_COLOR,
  current: "#0FA3CF",
};
