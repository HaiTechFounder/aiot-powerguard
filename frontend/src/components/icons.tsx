/**
 * The icon set, inline.
 *
 * A handful of 24px line glyphs is not worth a dependency, and inlining them
 * keeps them tintable with `currentColor` — which is what lets one KPI icon
 * sit on an azure card and the next on a white one without a second asset.
 */

import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 20, children, ...rest }: IconProps & { children: ReactNode }): ReactNode {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

/** The brand mark: a geometric shield with a bolt through it. */
export function LogoMark({ size = 36 }: { size?: number }): ReactNode {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      role="img"
      aria-label="PowerGuard"
      focusable="false"
    >
      <defs>
        <linearGradient id="pg-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#66CCFF" />
          <stop offset="100%" stopColor="#007FFF" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="36" height="36" rx="11" fill="url(#pg-logo)" />
      <path
        d="M21.6 9.5 12.4 21.2h6.1l-1.5 9.3 9.2-11.7h-6.1z"
        fill="#FFFFFF"
        stroke="#FFFFFF"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function GridIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <rect x="3" y="3" width="7.5" height="7.5" rx="2" />
      <rect x="13.5" y="3" width="7.5" height="7.5" rx="2" />
      <rect x="3" y="13.5" width="7.5" height="7.5" rx="2" />
      <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2" />
    </Svg>
  );
}

export function ChipIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <rect x="7" y="7" width="10" height="10" rx="2" />
      <path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4" />
    </Svg>
  );
}

export function AlertIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <path d="M12 4.5 2.8 20h18.4z" />
      <path d="M12 10v4.2M12 17.2h.01" />
    </Svg>
  );
}

export function BoltIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <path d="M13.5 3 5 14h6l-.5 7L19 10h-6z" />
    </Svg>
  );
}

export function WaveIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <path d="M2 12c2.2 0 2.2-6 4.4-6S8.6 18 10.8 18 13 6 15.2 6 17.4 12 19.6 12H22" />
    </Svg>
  );
}

export function PowerIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <path d="M12 3v8" />
      <path d="M6.8 6.6a8 8 0 1 0 10.4 0" />
    </Svg>
  );
}

export function BatteryIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <rect x="2" y="7" width="16" height="10" rx="2.5" />
      <path d="M21 10.5v3" />
      <path d="M6.5 10v4M10 10v4" />
    </Svg>
  );
}

export function ClockIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5.2l3.2 2" />
    </Svg>
  );
}

export function ActivityIcon(props: IconProps): ReactNode {
  return (
    <Svg {...props}>
      <path d="M2.5 12.5h4L9 5.5l4.5 13 2.5-6h5.5" />
    </Svg>
  );
}
