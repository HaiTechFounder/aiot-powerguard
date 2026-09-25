/**
 * A KPI card's sparkline — drawn only from readings that exist.
 *
 * Two points is the minimum that can honestly be called a trend, so below
 * that nothing is drawn at all. There is no smoothing, no projection and no
 * baseline at zero: the line spans the real minimum and maximum of the window
 * it was given, and a flat series draws flat.
 */

import type { ReactNode } from "react";

export function Sparkline({
  values,
  stroke,
  fill,
  label,
}: {
  values: readonly number[];
  stroke: string;
  fill: string;
  label: string;
}): ReactNode {
  const points = values.filter((value) => Number.isFinite(value));
  if (points.length < 2) return null;

  const width = 100;
  const height = 32;
  const min = Math.min(...points);
  const max = Math.max(...points);
  // A flat series would divide by zero; it draws down the middle instead.
  const span = max - min || 1;
  const step = width / (points.length - 1);

  const coords = points.map((value, index) => {
    const x = index * step;
    const y = height - ((value - min) / span) * (height - 4) - 2;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  return (
    <svg
      className="metric-card__spark"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
      focusable="false"
    >
      <polygon points={`0,${height} ${coords.join(" ")} ${width},${height}`} fill={fill} />
      <polyline
        points={coords.join(" ")}
        fill="none"
        stroke={stroke}
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
