/**
 * The WebSocket envelopes, against frames the backend actually built.
 *
 * `openapi.json` says nothing about `/ws/v1`: the frames are assembled in
 * `realtime/events.py`, so the fixture is a captured envelope of each type,
 * produced by those functions. Refresh it with the command in
 * `frontend/README.md`.
 *
 * This is where the first version of this dashboard was wrong. It typed an
 * `anomaly` frame as a full `AnomalyDto`, measurements included — but
 * `anomaly_event` does not send them. Nothing failed, because nothing
 * compared the two; the table simply rendered `undefined` as a reading. The
 * check below is what makes that a build failure rather than a screenshot.
 */

import { describe, expect, it } from "vitest";

import enums from "../fixtures/enums.json";
import frames from "../fixtures/ws_events.json";
import type {
  AnomalyEventData,
  AnomalyEvent,
  DeviceEvent,
  StatusEventData,
  TelemetryDto,
  TelemetryEvent,
} from "../../src/api/contract";
import { isDeviceEvent } from "../../src/realtime/socket";
import type { SchemaSpec, Shape } from "./binding";
import { CLEAN, assertAssignableType, assertExactType, checkPayload } from "./binding";

/** A `telemetry` frame: a stored row, minus the anomaly REST joins on. */
const telemetryFrameSpec = {
  id: { json: "integer" },
  device_id: { json: "string" },
  boot_id: { json: "string" },
  seq: { json: "integer" },
  sampled_at: { json: "string", nullable: true },
  received_at: { json: "string" },
  voltage_v: { json: "number" },
  current_a: { json: "number" },
  power_w: { json: "number" },
  energy_wh: { json: "number" },
  sensor_status: { json: "string" },
} as const satisfies SchemaSpec;

/** An `anomaly` frame: the verdict, and deliberately not the measurements. */
const anomalyFrameSpec = {
  id: { json: "integer" },
  telemetry_id: { json: "integer" },
  device_id: { json: "string" },
  detected_at: { json: "string" },
  method: { json: "string" },
  score: { json: "number", nullable: true },
  model_version: { json: "string", nullable: true },
  reasons: { json: "array", items: { json: "string" } },
} as const satisfies SchemaSpec;

const statusFrameSpec = {
  device_id: { json: "string" },
  status: { json: "string", values: ["online", "offline", "stale"] },
  last_seen_at: { json: "string" },
} as const satisfies SchemaSpec;

describe("every frame payload is exactly what its descriptor says", () => {
  it("telemetry carries a row without the REST-only anomaly join", () => {
    expect(
      assertExactType<Shape<typeof telemetryFrameSpec>, Omit<TelemetryDto, "anomaly">>(true),
    ).toBe(true);
  });

  it("a telemetry frame is still usable everywhere a TelemetryDto is", () => {
    // Which is why the series can hold rows from both transports.
    expect(assertAssignableType<Shape<typeof telemetryFrameSpec>, TelemetryDto>(true)).toBe(true);
  });

  it("anomaly carries the verdict alone", () => {
    expect(assertExactType<Shape<typeof anomalyFrameSpec>, AnomalyEventData>(true)).toBe(true);
  });

  it("status carries the badge and when it was last seen", () => {
    expect(assertExactType<Shape<typeof statusFrameSpec>, StatusEventData>(true)).toBe(true);
  });

  it("binds each envelope's `data` to the payload type it declares", () => {
    expect(assertExactType<TelemetryEvent["data"], TelemetryDto>(true)).toBe(true);
    expect(assertExactType<AnomalyEvent["data"], AnomalyEventData>(true)).toBe(true);
  });
});

describe("a captured frame", () => {
  const cases: [string, SchemaSpec, Record<string, unknown>][] = [
    ["telemetry", telemetryFrameSpec, frames.telemetry.data],
    ["anomaly", anomalyFrameSpec, frames.anomaly.data],
    ["status", statusFrameSpec, frames.status.data],
  ];

  it.each(cases)("%s matches the client's payload type", (_name, spec, payload) => {
    expect(checkPayload(spec, payload)).toEqual(CLEAN);
  });

  it.each(cases)("%s is wrapped in the v1 envelope", (name) => {
    const envelope = (frames as Record<string, Record<string, unknown>>)[name];
    expect(envelope?.schema_version).toBe(enums.ws_schema_version);
    expect(envelope?.type).toBe(name);
    expect(typeof envelope?.emitted_at).toBe("string");
  });

  it.each(cases)("%s passes the production envelope guard", (name) => {
    const envelope = (frames as Record<string, unknown>)[name];
    expect(isDeviceEvent(envelope)).toBe(true);
  });

  it("carries no measurements on an anomaly, whatever REST does", () => {
    // The regression this file exists for: if the backend ever adds them,
    // `checkPayload` reports them as unexpected and the client can start
    // reading them deliberately rather than by accident.
    for (const field of ["voltage_v", "current_a", "power_w", "energy_wh"]) {
      expect(Object.keys(frames.anomaly.data)).not.toContain(field);
    }
  });
});

describe("the frame types the client handles", () => {
  it("covers every type the backend can emit, and no more", () => {
    const handled: DeviceEvent["type"][] = ["telemetry", "anomaly", "status"];
    expect(new Set(enums.EventType)).toEqual(new Set(handled));
  });

  it("rejects a version the client was not written for", () => {
    expect(isDeviceEvent({ ...frames.telemetry, schema_version: 2 })).toBe(false);
  });
});
