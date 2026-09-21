import type { ReactNode } from "react";

import type { DeviceStatus } from "../api/contract";

const LABELS: Record<DeviceStatus, string> = {
  online: "Online",
  offline: "Offline",
  stale: "Stale",
};

/**
 * `stale` means the device stopped reporting without saying goodbye; `offline`
 * means it said so. Keeping them distinct is the point of the badge.
 */
export function StatusBadge({ status }: { status: DeviceStatus }): ReactNode {
  return (
    <span className={`badge badge--${status}`} data-testid="status-badge">
      {LABELS[status]}
    </span>
  );
}
