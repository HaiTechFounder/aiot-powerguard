import { Suspense, lazy, type ReactNode } from "react";
import { Route, Routes } from "react-router-dom";

import type { HealthDto } from "./api/contract";
import type { ApiError } from "./api/errors";
import { AppSidebar } from "./components/AppSidebar";
import { HealthHeader } from "./components/HealthHeader";
import { useHealth } from "./hooks/useHealth";
import { AnomaliesView } from "./views/AnomaliesView";
import { DevicesView } from "./views/DevicesView";

/**
 * The device dashboard is the only view that draws charts, and Recharts (with
 * its d3 dependencies) is most of the bundle. Loading that route on demand
 * keeps the device list and the anomaly history from paying for it.
 */
const DeviceDetailView = lazy(() =>
  import("./views/DeviceDetailView").then((module) => ({ default: module.DeviceDetailView })),
);

function ViewLoading(): ReactNode {
  return (
    <p className="state" role="status">
      Loading dashboard…
    </p>
  );
}

/**
 * The shell: navy rail on the left, masthead and view to the right of it.
 *
 * The sidebar sits inside `Routes` so that it can read `:deviceId` from the
 * matched route — that is what lets it disable the two device-scoped entries
 * honestly instead of linking somewhere that does not resolve. Health is read
 * once, at the top, and handed to both the rail and the masthead so the two
 * can never disagree about what the backend said.
 */
function Shell({
  health,
  healthError,
  children,
}: {
  health: HealthDto | null;
  healthError: ApiError | null;
  children: ReactNode;
}): ReactNode {
  return (
    <div className="app">
      <AppSidebar health={health} healthError={healthError} />
      <div className="app__content">
        <main className="main">
          <HealthHeader health={health} error={healthError} />
          {children}
        </main>
      </div>
    </div>
  );
}

export function App(): ReactNode {
  const health = useHealth();

  const shell = (children: ReactNode): ReactNode => (
    <Shell health={health.data} healthError={health.error}>
      {children}
    </Shell>
  );

  return (
    <Routes>
      <Route path="/" element={shell(<DevicesView />)} />
      <Route
        path="/devices/:deviceId"
        element={shell(
          <Suspense fallback={<ViewLoading />}>
            <DeviceDetailView />
          </Suspense>,
        )}
      />
      <Route path="/devices/:deviceId/anomalies" element={shell(<AnomaliesView />)} />
      <Route
        path="*"
        element={shell(<p className="state state--empty">That page does not exist.</p>)}
      />
    </Routes>
  );
}
