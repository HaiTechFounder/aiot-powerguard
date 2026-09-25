/**
 * The views, against a mocked backend.
 *
 * Every view gets all four states, and the honesty rule gets its own tests:
 * while the detector is unavailable the dashboard must say so rather than
 * reporting a clean bill of health from something that is switched off.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
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
import {
  BOOT_ID,
  DEVICE_ID,
  anomaly,
  device,
  health,
  telemetry,
  telemetryPage,
} from "../builders";
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

  // The banner reports the transport and nothing else. It used to say "Live",
  // which is a claim about the device that an open socket cannot support.
  it("reports the socket as connected, without claiming the device is live", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));

    act(() => FakeSocket.latest.open());

    await waitFor(() =>
      expect(screen.getByTestId("connection-banner")).toHaveTextContent("WebSocket connected"),
    );
    // The fixture's newest reading is months old, so the device verdict is not Live.
    expect(screen.getByTestId("live-state")).not.toHaveTextContent("Live");
  });

  it("says live updates stopped, and keeps the history, on a permanent close", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));

    act(() => {
      FakeSocket.latest.open();
      FakeSocket.latest.serverClose(4404);
    });

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

describe("the shell navigation", () => {
  it("offers only the three routes that exist, and disables the device-scoped two", async () => {
    server.use(...baseHandlers());
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId("sidebar")).toBeInTheDocument());
    const nav = screen.getByTestId("sidebar");
    // Overview is reachable without a device; the other two are not, so they
    // are inert text rather than links to a URL that would not resolve.
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Devices" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Anomaly History" })).not.toBeInTheDocument();
    expect(nav).toHaveTextContent("Devices");
    expect(nav).toHaveTextContent("Anomaly History");
  });

  it("loads the chart dashboard on demand and still renders it through the shell", async () => {
    server.use(...baseHandlers());
    render(
      <MemoryRouter initialEntries={[`/devices/${DEVICE_ID}`]}>
        <App />
      </MemoryRouter>,
    );

    // The lazily loaded route resolves to the real view, not a stuck fallback.
    await waitFor(() => expect(screen.getByTestId("live-state")).toBeInTheDocument());
    expect(screen.queryByText("Loading dashboard…")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("chart-voltage")).toBeInTheDocument());
  });

  it("links the device-scoped entries once a device is in the route", async () => {
    server.use(...baseHandlers());
    render(
      <MemoryRouter initialEntries={[`/devices/${DEVICE_ID}`]}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Anomaly History" })).toHaveAttribute(
        "href",
        `/devices/${DEVICE_ID}/anomalies`,
      ),
    );
    expect(screen.getByRole("link", { name: "Devices" })).toHaveAttribute(
      "href",
      `/devices/${DEVICE_ID}`,
    );
  });
});

describe("the device information rail", () => {
  const path = `/devices/${DEVICE_ID}`;

  it("shows the properties the API actually sends, and nothing else", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByTestId("device-info")).toBeInTheDocument());
    const rail = screen.getByTestId("device-info");
    expect(rail).toHaveTextContent("0.1.0");

    const sensor = screen.getByTestId("sensor-info");
    expect(sensor).toHaveTextContent("ok");
    expect(sensor).toHaveTextContent(BOOT_ID);
    // `sampled_at` is null until the firmware has a clock; it says so.
    expect(sensor).toHaveTextContent("not reported");

    // No invented telemetry about the device itself.
    const shown = `${rail.textContent ?? ""}${sensor.textContent ?? ""}`;
    expect(shown).not.toMatch(/RSSI|uptime|IP address/i);
  });

  it("says the registry could not be read instead of inventing properties", async () => {
    server.use(
      http.get("*/api/v1/devices", () => envelope("HTTP_ERROR", "registry down", 503)),
      ...baseHandlers(),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() =>
      expect(screen.getByTestId("device-props-unavailable")).toBeInTheDocument(),
    );
    // The live area is unaffected by a failed registry read.
    expect(screen.getByTestId("metric-voltage")).toBeInTheDocument();
  });
});

describe("the live telemetry chart", () => {
  it("plots voltage and current on separately labelled axes", async () => {
    server.use(...baseHandlers());
    renderAt(`/devices/${DEVICE_ID}`, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByTestId("chart-live")).toBeInTheDocument());
    // Volts and amps are different quantities; the legend names which axis
    // each line is read against.
    expect(screen.getByText(/Voltage — left axis \(V\)/)).toBeInTheDocument();
    expect(screen.getByText(/Current — right axis \(A\)/)).toBeInTheDocument();
  });
});

describe("the system information card", () => {
  const path = `/devices/${DEVICE_ID}`;

  it("prints the health states the API returned, not a green default", async () => {
    server.use(
      http.get("*/api/v1/health", () =>
        HttpResponse.json(health({ mqtt: "disconnected", model: "unavailable" })),
      ),
      ...baseHandlers(),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByTestId("system-info")).toBeInTheDocument());
    const panel = screen.getByTestId("system-info");
    expect(panel).toHaveTextContent("disconnected");
    expect(panel).toHaveTextContent("unavailable");
    expect(panel).toHaveTextContent("0.1.0");
  });
});

describe("the reading-window control", () => {
  const path = `/devices/${DEVICE_ID}`;

  it("is not offered when no window would filter anything", async () => {
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByTestId("chart-live")).toBeInTheDocument());
    // Five rows: a "Last 60" button would drop nothing, so it is not shown.
    expect(screen.queryByRole("button", { name: "Last 60" })).not.toBeInTheDocument();
  });

  it("narrows the live chart to the readings actually held", async () => {
    server.use(
      http.get("*/api/v1/devices/:id/telemetry", () =>
        HttpResponse.json({ items: telemetryPage(80) }),
      ),
      ...baseHandlers(),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() => expect(screen.getByText(/80 readings shown/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Last 60" }));

    expect(screen.getByText(/60 readings shown/)).toBeInTheDocument();
    // The window is a view over loaded rows; it never refetches or invents any.
    expect(screen.getByRole("button", { name: "All 80" })).toBeInTheDocument();
  });
});

describe("the live badge on the device dashboard", () => {
  const path = `/devices/${DEVICE_ID}`;

  /** A device page whose newest reading arrived just now. */
  function freshHandlers(overrides: { mqtt?: string; status?: "online" | "offline" | "stale" } = {}) {
    const fresh = telemetry({ id: 900, received_at: new Date().toISOString() });
    return [
      http.get("*/api/v1/health", () =>
        HttpResponse.json(health({ mqtt: overrides.mqtt ?? "connected", model: "unavailable" })),
      ),
      http.get("*/api/v1/devices", () =>
        HttpResponse.json({ items: [device({ status: overrides.status ?? "online" })] }),
      ),
      http.get("*/api/v1/devices/:id/telemetry", () => HttpResponse.json({ items: [fresh] })),
      http.get("*/api/v1/devices/:id/anomalies", () => HttpResponse.json({ items: [] })),
    ];
  }

  async function openSocket() {
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
    act(() => FakeSocket.latest.open());
  }

  it("says Live when backend, broker, socket, device and freshness all hold", async () => {
    server.use(...freshHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await openSocket();

    await waitFor(() => expect(screen.getByTestId("live-state")).toHaveTextContent("Live"));
    expect(screen.getByTestId("live-state")).toHaveAttribute("data-level", "live");
    expect(screen.getByTestId("metric-provenance")).toHaveTextContent("Live measurements");
    expect(screen.getByRole("heading", { name: "Live Telemetry" })).toBeInTheDocument();
  });

  it("renames the chart the moment the verdict drops, with no new reading", async () => {
    server.use(...freshHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await openSocket();
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Live Telemetry" })).toBeInTheDocument(),
    );

    // Only the device status changes: the series is untouched.
    act(() =>
      FakeSocket.latest.deliver({
        schema_version: 1,
        type: "status",
        emitted_at: new Date().toISOString(),
        data: { device_id: DEVICE_ID, status: "offline", last_seen_at: new Date().toISOString() },
      }),
    );

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Telemetry history" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("heading", { name: "Live Telemetry" })).not.toBeInTheDocument();
  });

  it("never says Live while the broker is disconnected, though the socket is open", async () => {
    server.use(...freshHandlers({ mqtt: "disconnected" }));
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await openSocket();

    await waitFor(() =>
      expect(screen.getByTestId("live-state")).toHaveTextContent("Broker disconnected"),
    );
    expect(screen.getByTestId("connection-banner")).toHaveTextContent("WebSocket connected");
    expect(screen.getByTestId("live-state")).not.toHaveAttribute("data-level", "live");
  });

  it("never says Live while the device is offline, and keeps its history on screen", async () => {
    server.use(...freshHandlers({ status: "offline" }));
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await openSocket();

    await waitFor(() =>
      expect(screen.getByTestId("live-state")).toHaveTextContent("Device offline"),
    );
    // An outage hides nothing that was already loaded.
    expect(screen.getByTestId("chart-voltage")).toBeInTheDocument();
    expect(screen.getByTestId("metric-voltage")).toHaveTextContent("7.840 V");
  });

  it("calls an old reading stale and labels the KPIs as history, not measurements", async () => {
    // The shared fixtures are stamped January 2026, so they are long stale.
    server.use(...baseHandlers());
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");
    await openSocket();

    await waitFor(() =>
      expect(screen.getByTestId("live-state")).toHaveTextContent("Stale data"),
    );
    expect(screen.getByTestId("metric-provenance")).toHaveTextContent("Last known readings");
    expect(screen.getByTestId("metric-provenance")).not.toHaveTextContent("Live measurements");
    // The chart over that history must not call itself live either.
    expect(screen.getByRole("heading", { name: "Telemetry history" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Live Telemetry" })).not.toBeInTheDocument();
    expect(screen.getByTestId("live-state-last-received")).toBeInTheDocument();
  });

  it("says the backend is unavailable when health cannot be read", async () => {
    server.use(
      http.get("*/api/v1/health", () => HttpResponse.error()),
      ...baseHandlers(),
    );
    renderAt(path, <DeviceDetailView />, "/devices/:deviceId");

    await waitFor(() =>
      expect(screen.getByTestId("live-state")).toHaveTextContent("Backend unavailable"),
    );
  });
});
