/**
 * The TypeScript DTOs against the backend's published schemas.
 *
 * `binding.ts` explains the mechanism. The short version: each descriptor
 * below is checked against the DTO by the compiler and against
 * `fixtures/openapi.json` by these tests, so neither side can move alone.
 *
 * To see it work, change any DTO in `src/api/contract.ts` — rename a field,
 * make a required one optional, turn a `number` into a `string` — and run
 * `npm run typecheck`. Change the descriptor to match and these tests fail
 * instead.
 */

import { describe, expect, it } from "vitest";

import enums from "../fixtures/enums.json";
import envelopes from "../fixtures/error_envelope.json";
import spec from "../fixtures/openapi.json";
import type {
  AnomalyDto,
  AnomalySummary,
  AnomalyPageDto,
  DeviceDto,
  DeviceListDto,
  ErrorEnvelope,
  HealthDto,
  TelemetryDto,
  TelemetryPageDto,
} from "../../src/api/contract";
import type {
  fetchAnomalies,
  fetchDevices,
  fetchHealth,
  fetchLatest,
  fetchTelemetry,
} from "../../src/api/client";
import type { SchemaSpec, Shape } from "./binding";
import { CLEAN, assertExactType, checkSchema, findings } from "./binding";

interface PublishedSchema {
  properties?: Record<string, unknown>;
  required?: string[];
  additionalProperties?: boolean;
}

const schemas = (spec as { components: { schemas: Record<string, PublishedSchema> } }).components
  .schemas;

// -- descriptors -------------------------------------------------------------

const healthSpec = {
  status: { json: "string" },
  database: { json: "string" },
  mqtt: { json: "string" },
  model: { json: "string" },
  version: { json: "string" },
} as const satisfies SchemaSpec;

const telemetrySpec = {
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
  anomaly: { json: "ref", ref: "AnomalySummary", nullable: true, optional: true },
} as const satisfies SchemaSpec;

const anomalySummarySpec = {
  id: { json: "integer" },
  method: { json: "string" },
  score: { json: "number", nullable: true },
  model_version: { json: "string", nullable: true },
  reasons: { json: "array", items: { json: "string" } },
} as const satisfies SchemaSpec;

const anomalySpec = {
  id: { json: "integer" },
  telemetry_id: { json: "integer" },
  device_id: { json: "string" },
  detected_at: { json: "string" },
  method: { json: "string" },
  score: { json: "number", nullable: true },
  model_version: { json: "string", nullable: true },
  reasons: { json: "array", items: { json: "string" } },
  voltage_v: { json: "number" },
  current_a: { json: "number" },
  power_w: { json: "number" },
  energy_wh: { json: "number" },
} as const satisfies SchemaSpec;

const deviceSpec = {
  id: { json: "string" },
  firmware_version: { json: "string" },
  // The backend publishes a bare string; the client narrows it, and
  // `fixtures/enums.json` holds the backend to that narrowing.
  status: { json: "string", values: ["online", "offline", "stale"] },
  first_seen_at: { json: "string" },
  last_seen_at: { json: "string" },
  latest: { json: "ref", ref: "TelemetryDto", nullable: true, optional: true },
} as const satisfies SchemaSpec;

const deviceListSpec = {
  items: { json: "array", items: { json: "ref", ref: "DeviceDto" } },
} as const satisfies SchemaSpec;

const telemetryPageSpec = {
  items: { json: "array", items: { json: "ref", ref: "TelemetryDto" } },
  next_before_id: { json: "integer", nullable: true, optional: true },
} as const satisfies SchemaSpec;

const anomalyPageSpec = {
  items: { json: "array", items: { json: "ref", ref: "AnomalyDto" } },
  next_before_id: { json: "integer", nullable: true, optional: true },
} as const satisfies SchemaSpec;

// -- the compiler's half -----------------------------------------------------

describe("every DTO is exactly what its descriptor says", () => {
  // Each case fails at *compile* time if the DTO and the descriptor disagree
  // on a name, a type or whether a field may be absent. The runtime assertion
  // is a formality; `npm run typecheck` is where the drift is caught.
  it("HealthDto", () => {
    expect(assertExactType<Shape<typeof healthSpec>, HealthDto>(true)).toBe(true);
  });
  it("TelemetryDto", () => {
    expect(assertExactType<Shape<typeof telemetrySpec>, TelemetryDto>(true)).toBe(true);
  });
  it("AnomalySummary", () => {
    expect(assertExactType<Shape<typeof anomalySummarySpec>, AnomalySummary>(true)).toBe(true);
  });
  it("AnomalyDto", () => {
    expect(assertExactType<Shape<typeof anomalySpec>, AnomalyDto>(true)).toBe(true);
  });
  it("DeviceDto", () => {
    expect(assertExactType<Shape<typeof deviceSpec>, DeviceDto>(true)).toBe(true);
  });
  it("DeviceListDto", () => {
    expect(assertExactType<Shape<typeof deviceListSpec>, DeviceListDto>(true)).toBe(true);
  });
  it("TelemetryPageDto", () => {
    expect(assertExactType<Shape<typeof telemetryPageSpec>, TelemetryPageDto>(true)).toBe(true);
  });
  it("AnomalyPageDto", () => {
    expect(assertExactType<Shape<typeof anomalyPageSpec>, AnomalyPageDto>(true)).toBe(true);
  });
});

describe("every endpoint returns the wrapper the client reads", () => {
  // The response wrapper, bound to the client function itself: a helper that
  // starts returning `TelemetryDto[]` instead of `TelemetryPageDto` stops
  // compiling here.
  it("GET /health", () => {
    expect(assertExactType<Awaited<ReturnType<typeof fetchHealth>>, HealthDto>(true)).toBe(true);
  });
  it("GET /devices", () => {
    expect(assertExactType<Awaited<ReturnType<typeof fetchDevices>>, DeviceListDto>(true)).toBe(
      true,
    );
  });
  it("GET /devices/{id}/latest", () => {
    expect(assertExactType<Awaited<ReturnType<typeof fetchLatest>>, TelemetryDto>(true)).toBe(true);
  });
  it("GET /devices/{id}/telemetry", () => {
    expect(
      assertExactType<Awaited<ReturnType<typeof fetchTelemetry>>, TelemetryPageDto>(true),
    ).toBe(true);
  });
  it("GET /devices/{id}/anomalies", () => {
    expect(
      assertExactType<Awaited<ReturnType<typeof fetchAnomalies>>, AnomalyPageDto>(true),
    ).toBe(true);
  });
});

// -- the runtime half --------------------------------------------------------

const BOUND: [string, SchemaSpec][] = [
  ["HealthDto", healthSpec],
  ["TelemetryDto", telemetrySpec],
  ["AnomalySummary", anomalySummarySpec],
  ["AnomalyDto", anomalySpec],
  ["DeviceDto", deviceSpec],
  ["DeviceListDto", deviceListSpec],
  ["TelemetryPageDto", telemetryPageSpec],
  ["AnomalyPageDto", anomalyPageSpec],
];

describe("every DTO the dashboard reads", () => {
  it.each(BOUND)("%s matches the published schema exactly", (name, descriptor) => {
    const report = checkSchema(descriptor, schemas[name]);
    expect(findings(report)).toEqual(CLEAN);
  });

  it.each(BOUND)("%s is closed, so an undeclared field cannot appear", (name) => {
    expect(checkSchema({}, schemas[name]).closed).toBe(true);
  });
});

describe("pagination", () => {
  it("keeps `next_before_id` optional and nullable on both pages", () => {
    for (const name of ["TelemetryPageDto", "AnomalyPageDto"]) {
      const schema = schemas[name] as PublishedSchema;
      expect(schema.required ?? [], `${name}.next_before_id became required`).not.toContain(
        "next_before_id",
      );
      expect(Object.keys(schema.properties ?? {})).toContain("next_before_id");
    }
  });

  it("keeps `items` required, because the client reads it without a guard", () => {
    for (const name of ["TelemetryPageDto", "AnomalyPageDto", "DeviceListDto"]) {
      expect(schemas[name]?.required ?? []).toContain("items");
    }
  });
});

describe("the values the client narrows", () => {
  it("still knows every device status the backend can send", () => {
    expect(new Set(enums.DeviceStatus)).toEqual(new Set(deviceSpec.status.values));
  });

  it("renders the anomaly method as free text, so a new one cannot break it", () => {
    // Not narrowed on purpose: `AnomalyMethod` grows when ML lands in Phase 05.
    expect(enums.AnomalyMethod.length).toBeGreaterThan(0);
    expect(anomalySpec.method.json).toBe("string");
  });
});

describe("the error envelope", () => {
  // Exception handlers are not in `openapi.json`, so these are captured from
  // the running backend instead — same idea, different source of truth.
  const notFound: ErrorEnvelope = envelopes.not_found;
  const validation: ErrorEnvelope = envelopes.validation;

  it("carries the fields the client renders", () => {
    for (const envelope of [notFound, validation]) {
      expect(typeof envelope.error.code).toBe("string");
      expect(typeof envelope.error.message).toBe("string");
      expect(Object.keys(envelope)).toEqual(["error"]);
    }
  });

  it("says which device is unknown, rather than a bare status", () => {
    expect(notFound.error.message).toMatch(/unknown device/);
  });

  it("carries field detail on a validation failure", () => {
    expect(validation.error.details).not.toBeNull();
  });
});
