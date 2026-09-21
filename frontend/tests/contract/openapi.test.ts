/**
 * The published API surface: endpoints, parameters and response wrappers.
 *
 * The *shape* of every DTO is checked in `dto.test.ts`, bound to the
 * TypeScript types themselves. This file covers what surrounds them — which
 * paths exist, which are read-only, what each returns, and whether the query
 * parameters the client builds are still accepted.
 *
 * `tests/fixtures/openapi.json` is exported from the running backend; the
 * verification gate re-exports it and fails on a diff, so a stale fixture
 * cannot hide a contract change. Refresh it with the command in
 * `frontend/README.md`.
 */

import { describe, expect, it } from "vitest";

import spec from "../fixtures/openapi.json";
import { LIMIT_MAX, LIMIT_MIN } from "../../src/api/contract";

interface Operation {
  parameters?: { name: string; required?: boolean; schema?: unknown }[];
  responses?: Record<string, { content?: Record<string, { schema?: { $ref?: string } }> }>;
}

const paths = (spec as { paths: Record<string, Record<string, Operation>> }).paths;

const TELEMETRY = "/api/v1/devices/{device_id}/telemetry";
const ANOMALIES = "/api/v1/devices/{device_id}/anomalies";

function operation(path: string): Operation {
  const entry = paths[path]?.get;
  if (!entry) throw new Error(`${path} is no longer published`);
  return entry;
}

function okSchema(path: string): string | undefined {
  return operation(path).responses?.["200"]?.content?.["application/json"]?.schema?.$ref?.replace(
    "#/components/schemas/",
    "",
  );
}

function parameterNames(path: string): Set<string> {
  return new Set((operation(path).parameters ?? []).map((parameter) => parameter.name));
}

describe("the published REST contract", () => {
  it("exposes exactly the five read endpoints the dashboard uses", () => {
    expect(new Set(Object.keys(paths))).toEqual(
      new Set([
        "/api/v1/health",
        "/api/v1/devices",
        "/api/v1/devices/{device_id}/latest",
        TELEMETRY,
        ANOMALIES,
      ]),
    );
  });

  it("publishes no mutating operation", () => {
    // The dashboard is read-only by design; a POST appearing here is a
    // contract change that needs a decision, not a silent new capability.
    for (const operations of Object.values(paths)) {
      expect(Object.keys(operations)).toEqual(["get"]);
    }
  });

  it.each([
    ["/api/v1/health", "HealthDto"],
    ["/api/v1/devices", "DeviceListDto"],
    ["/api/v1/devices/{device_id}/latest", "TelemetryDto"],
    [TELEMETRY, "TelemetryPageDto"],
    [ANOMALIES, "AnomalyPageDto"],
  ])("%s still answers with %s", (path, schema) => {
    expect(okSchema(path)).toBe(schema);
  });
});

describe("history query parameters", () => {
  it.each([TELEMETRY, ANOMALIES])("%s accepts every parameter the client builds", (path) => {
    const names = parameterNames(path);
    for (const name of ["from", "to", "limit", "before_id"]) {
      expect(names, `${name} is no longer accepted`).toContain(name);
    }
  });

  it.each([TELEMETRY, ANOMALIES])("%s keeps them all optional", (path) => {
    const optional = (operation(path).parameters ?? []).filter(
      (parameter) => parameter.name !== "device_id",
    );
    for (const parameter of optional) {
      expect(parameter.required ?? false, `${parameter.name} became mandatory`).toBe(false);
    }
  });

  it.each([TELEMETRY, ANOMALIES])("%s agrees with the client's limit bounds", (path) => {
    const limit = (operation(path).parameters ?? []).find(
      (parameter) => parameter.name === "limit",
    );
    const schema = limit?.schema as
      | { maximum?: number; minimum?: number; anyOf?: { maximum?: number; minimum?: number }[] }
      | undefined;
    // FastAPI nests the bounds under anyOf when the parameter is optional.
    const bounds =
      schema?.maximum !== undefined
        ? schema
        : (schema?.anyOf?.find((entry) => entry.maximum !== undefined) ?? {});
    expect(bounds.minimum).toBe(LIMIT_MIN);
    expect(bounds.maximum).toBe(LIMIT_MAX);
  });
});
