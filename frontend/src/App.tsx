import type { ReactNode } from "react";
import { Route, Routes } from "react-router-dom";

import { HealthHeader } from "./components/HealthHeader";
import { useHealth } from "./hooks/useHealth";
import { AnomaliesView } from "./views/AnomaliesView";
import { DeviceDetailView } from "./views/DeviceDetailView";
import { DevicesView } from "./views/DevicesView";

export function App(): ReactNode {
  const health = useHealth();

  return (
    <div className="app">
      <HealthHeader health={health.data} error={health.error} />
      <main className="main">
        <Routes>
          <Route path="/" element={<DevicesView />} />
          <Route path="/devices/:deviceId" element={<DeviceDetailView />} />
          <Route path="/devices/:deviceId/anomalies" element={<AnomaliesView />} />
          <Route
            path="*"
            element={<p className="state state--empty">That page does not exist.</p>}
          />
        </Routes>
      </main>
    </div>
  );
}
