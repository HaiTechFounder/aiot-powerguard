/**
 * The views, against a mocked backend.
 *
 * Every view gets all four states, and the honesty rule gets its own tests:
 * while the detector is unavailable the dashboard must say so rather than
 * reporting a clean bill of health from something that is switched off.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { App } from "../../src/App";
import { AnomalyList } from "../../src/components/AnomalyList";
import { AnomaliesView } from "../../src/views/AnomaliesView";
import { DeviceDetailView } from "../../src/views/DeviceDetailView";
import { DevicesView } from "../../src/views/DevicesView";
import { DEVICE_ID, anomaly, device, health, telemetryPage } from "../builders";
import { FakeSocket } from "../fakeSocket";

const server = setupServer();

function envelope(code: string, message: string, status: number) {
  return HttpResponse.json({ error: { code, message, details: null } }, { status });
}

/**
 * Endpoints a view touches but does not assert on, so nothing 404s by accident.
 *
 * MSW resolves the first matching handler, so an override must be passed
 * *before* these — `server.use(override, ...baseHandlers())`.
 */
function baseHandlers(overrides: { model?: string } = {}) {
  return [
    http.get("*/api/v1/health", () =>
      HttpResponse.json(health({ model: overrides.model ?? "unavailable" })),
    ),
    http.get("*/api/v1/devices", () => HttpResponse.json({ items: [device()] })),
    http.get("*/api/v1/devices/:id/telemetry", () =>
      HttpResponse.json({ items: telemetryPage(5) }),
    ),
    http.get("*/api/v1/devices/:id/anomalies", () => HttpResponse.json({ items: [] })),
  ];
}

function renderAt(path: string, element: React.ReactNode, route: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={route} element={element} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

beforeEach(() => {
  FakeSocket.reset();
  // The views use the real WebSocket class; a fake keeps jsdom quiet and the
  // tests deterministic. Socket behaviour itself is covered in tests/unit.
  vi.stubGlobal("WebSocket", FakeSocket);
});

describe("the device list", () => {
  it("shows a spinner first", async () => {
    server.use(...baseHandlers());
    renderAt("/", <DevicesView />, "/");

    expect(screen.getByRole("status")).toHaveTextContent("Loading devices…");
    await waitFor(() => expect(screen.getByText(DEVICE_ID)).toBeInTheDocument());
  });

  it("shows devices with their status and latest reading", async () => {
    server.use(...baseHandlers());
    renderAt("/", <DevicesView />, "/");

    await waitFor(() => expect(screen.getByText(DEVICE_ID)).toBeInTheDocument());
    expect(screen.getByTestId("status-badge")).toHaveTextContent("Online");
    expect(screen.getByText("7.840 V")).toBeInTheDocument();
    expect(screen.getByText("3.269 W")).toBeInTheDocument();
  });

  it("treats no devices as empty, not as a failure", async () => {
    server.use(
      http.get("*/api/v1/devices", () => HttpResponse.json({ items: [] })),
      ...baseHandlers(),
    );
    renderAt("/", <DevicesView />, "/");

    await waitFor(() =>
      expect(screen.getByText("No devices have reported yet")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says a device has no readings rather than showing zeros", async () => {
    server.use(
      http.get("*/api/v1/devices", () =>
        HttpResponse.json({ items: [device({ latest: null })] }),
      ),
      ...baseHandlers(),
    );
    renderAt("/", <DevicesView />, "/");

    await waitFor(() => expect(screen.getByText("No readings yet")).toBeInTheDocument());
  });

  it("reports a backend failure and offers a retry", async () => {
    server.use(
      http.get("*/api/v1/devices", () => envelope("HTTP_ERROR", "database unavailable", 503)),
      ...baseHandlers(),
    );
    renderAt("/", <DevicesView />, "/");

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText("database unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("recovers when the retry succeeds", async () => {
    let failed = false;
    server.use(
      http.get("*/api/v1/devices", () => {
        if (!failed) {
          failed = true;
          return envelope("HTTP_ERROR", "temporary", 503);
        }
        return HttpResponse.json({ items: [device()] });
      }),
      ...baseHandlers(),
    );
    renderAt("/", <DevicesView />, "/");
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(screen.getByText(DEVICE_ID)).toBeInTheDocument());
  });
});

describe("the device detail view", () => {
  const path = `/devices/${DEVICE_ID}`;

  it("shows the latest values and a chart per metric", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByTestId("metric-voltage")).toHaveTextContent("7.840 V"));
    expect(screen.getByTestId("metric-current")).toHaveTextContent("0.417 A");
    expect(screen.getByTestId("metric-power")).toHaveTextContent("3.269 W");
    expect(screen.getByTestId("chart-voltage")).toBeInTheDocument();
    expect(screen.getByTestId("chart-current")).toBeInTheDocument();
    expect(screen.getByTestId("chart-power")).toBeInTheDocument();
  });

  it("says a device has no readings yet instead of drawing an empty chart", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/telemetry", () => HttpResponse.json({ items: [] })),
      ...baseHandlers(),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() =>
      expect(screen.getByText("No readings for this device yet")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("chart-voltage")).not.toBeInTheDocument();
  });

  it("reports a history failure without hiding the live area", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/telemetry", () =>
        envelope("NOT_FOUND", "unknown device: ghost", 404),
      ),
      ...baseHandlers(),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText("unknown device: ghost")).toBeInTheDocument();
    expect(screen.getByTestId("metric-voltage")).toBeInTheDocument();
  });

  it("shows the connection as live once the socket opens", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));

    FakeSocket.latest.open();

    await waitFor(() =>
      expect(screen.getByTestId("connection-banner")).toHaveTextContent("Live"),
    );
  });

  it("says live updates stopped, and keeps the history, on a permanent close", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));

    FakeSocket.latest.open();
    FakeSocket.latest.serverClose(4404);

    await waitFor(() =>
      expect(screen.getByTestId("connection-banner")).toHaveTextContent(
        /does not know this device/,
      ),
    );
    expect(screen.getByTestId("chart-voltage")).toBeInTheDocument();
  });

  it("says detection is unavailable, never that there are no anomalies", async () => {
    server.use(...baseHandlers({ model: "unavailable" }));
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() =>
      expect(screen.getByTestId("detection-unavailable")).toBeInTheDocument(),
    );
    expect(screen.getByText("Anomaly detection unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/No anomalies recorded/)).not.toBeInTheDocument();
  });

  it("shows a real empty result once a model is loaded", async () => {
    server.use(...baseHandlers({ model: "ready" }));
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByText("No anomalies recorded")).toBeInTheDocument());
    expect(screen.queryByTestId("detection-unavailable")).not.toBeInTheDocument();
  });

  // The list is billed as recorded history. A page that never asks REST for
  // the stored verdicts, then prints "no verdicts are stored for this device",
  // is not reporting an empty history — it is hiding a real one.
  it("lists verdicts stored by the backend, not only this session's frames", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/anomalies", () =>
        HttpResponse.json({ items: [anomaly({ id: 77 })] }),
      ),
      ...baseHandlers({ model: "unavailable" }),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByTestId("anomaly-table")).toBeInTheDocument());
    expect(screen.getByText("overcurrent_rule")).toBeInTheDocument();
    // The detector being off never suppresses what was already recorded.
    expect(screen.getByTestId("detection-unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/No recorded anomaly entries/)).not.toBeInTheDocument();
  });

  it("does not call an unread anomaly history an empty one", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/anomalies", () =>
        envelope("HTTP_ERROR", "anomaly query failed", 500),
      ),
      ...baseHandlers({ model: "ready" }),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() =>
      expect(screen.getByText("anomaly query failed")).toBeInTheDocument(),
    );
    expect(screen.queryByText("No anomalies recorded")).not.toBeInTheDocument();
    // The live view is unaffected by a failed anomaly read.
    expect(screen.getByTestId("metric-voltage")).toBeInTheDocument();
  });
});

describe("the anomaly view", () => {
  const path = `/devices/${DEVICE_ID}/anomalies`;
  const route = "/devices/:deviceId/anomalies";

  it("lists anomalies with the measurements behind them", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/anomalies", () =>
        HttpResponse.json({ items: [anomaly()] }),
      ),
      ...baseHandlers({ model: "ready" }),
    );
    renderAt(path, <AnomaliesView />, route);

    await waitFor(() => expect(screen.getByTestId("anomaly-table")).toBeInTheDocument());
    expect(screen.getByText("overcurrent_rule")).toBeInTheDocument();
    expect(screen.getByText("3.200 A")).toBeInTheDocument();
    expect(screen.getByText("0.91")).toBeInTheDocument();
  });

  it("shows a spinner, then the result", async () => {
    server.use(...baseHandlers({ model: "ready" }));
    renderAt(path, <AnomaliesView />, route);

    expect(screen.getByRole("status")).toHaveTextContent("Loading anomalies…");
    await waitFor(() => expect(screen.getByText("No anomalies recorded")).toBeInTheDocument());
  });

  it("reports a failure with a retry", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/anomalies", () =>
        envelope("HTTP_ERROR", "query failed", 500),
      ),
      ...baseHandlers({ model: "ready" }),
    );
    renderAt(path, <AnomaliesView />, route);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText("query failed")).toBeInTheDocument();
  });

  it("stays honest about an unavailable detector here too", async () => {
    server.use(...baseHandlers({ model: "unavailable" }));
    renderAt(path, <AnomaliesView />, route);

    await waitFor(() =>
      expect(screen.getByTestId("detection-unavailable")).toBeInTheDocument(),
    );
  });

  it("keeps persisted verdicts visible when model health becomes unavailable", () => {
    const records = [anomaly({ id: 42 })];
    const view = render(<AnomalyList anomalies={records} modelReady />);
    expect(screen.getByTestId("anomaly-table")).toBeInTheDocument();

    view.rerender(<AnomalyList anomalies={records} modelReady={false} />);
    expect(screen.getByTestId("detection-unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("anomaly-table")).toBeInTheDocument();
  });

  it("does not call an empty anomaly history a clean assessment", () => {
    render(<AnomalyList anomalies={[]} modelReady />);
    expect(screen.getByText("No anomalies recorded")).toBeInTheDocument();
    expect(screen.queryByText(/Every assessed reading looked normal/)).not.toBeInTheDocument();
  });
});

describe("the health header", () => {
  it("reports each subsystem separately", async () => {
    server.use(...baseHandlers());
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId("health-backend")).toHaveTextContent("ok"));
    expect(screen.getByTestId("health-database")).toHaveTextContent("ready");
    expect(screen.getByTestId("health-mqtt")).toHaveTextContent("connected");
    expect(screen.getByTestId("health-model")).toHaveTextContent("unavailable");
  });

  it("treats a broker outage as a state, not as a broken service", async () => {
    server.use(
      http.get("*/api/v1/health", () =>
        HttpResponse.json(health({ mqtt: "disconnected", model: "unavailable" })),
      ),
      ...baseHandlers(),
    );
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("health-mqtt")).toHaveTextContent("disconnected"),
    );
    expect(screen.getByTestId("health-backend")).toHaveTextContent("ok");
  });

  it("says the backend is unreachable when nothing answers", async () => {
    server.use(
      http.get("*/api/v1/health", () => HttpResponse.error()),
      ...baseHandlers(),
    );
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("health-backend")).toHaveTextContent("Backend unreachable"),
    );
  });
});
