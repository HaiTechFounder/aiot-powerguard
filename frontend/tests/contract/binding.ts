/**
 * The bridge between the backend's published contract and the client's types.
 *
 * A committed `openapi.json` proves what the backend sends. It proves nothing
 * about `src/api/contract.ts`, and a test that compares field *names* against a
 * hard-coded list proves less still: rename a property in the TypeScript DTO,
 * change its type, or make a required field optional, and such a test stays
 * green while the dashboard reads fields that will never arrive.
 *
 * So each DTO gets one descriptor, and the descriptor is pinned at both ends:
 *
 *   - **To TypeScript, by the compiler.** `Shape<typeof spec>` reconstructs an
 *     object type from the descriptor, and `AssertExact` fails the build unless
 *     it is *identical* to the DTO — same keys, same types, same optionality.
 *     Not assignable: identical. A renamed, retyped, added, removed or
 *     newly-optional property all break `npm run typecheck`.
 *   - **To the backend, at runtime.** `checkSchema` compares the same
 *     descriptor against the published schema: property set, required set,
 *     JSON types, nullability, array element types and `$ref` targets.
 *
 * Editing only the DTO breaks the compiler. Editing the DTO and the descriptor
 * together breaks the runtime comparison. There is no edit that drifts quietly.
 */

import type { AnomalyDto, AnomalySummary, DeviceDto, TelemetryDto } from "../../src/api/contract";

/** What a JSON property may be, in the vocabulary OpenAPI uses. */
export type JsonKind = "string" | "number" | "integer" | "boolean" | "ref" | "array";

export interface FieldSpec {
  readonly json: JsonKind;
  /** The schema name a `$ref` points at. */
  readonly ref?: string;
  /** The element descriptor, for `json: "array"`. */
  readonly items?: FieldSpec;
  /** `null` is an accepted value (OpenAPI renders it as `anyOf` with null). */
  readonly nullable?: boolean;
  /** Absent from the payload entirely, rather than present and null. */
  readonly optional?: boolean;
  /**
   * The exact set of values, when the client narrows a plain string to a
   * union. The backend publishes these as bare strings, so the union is an
   * assumption — one `tests/fixtures/enums.json` holds the backend to.
   */
  readonly values?: readonly string[];
}

export type SchemaSpec = Readonly<Record<string, FieldSpec>>;

// -- the type-level half -----------------------------------------------------

/** The schemas a `$ref` may point at, so a reference reconstructs to a type. */
export interface Registry {
  TelemetryDto: TelemetryDto;
  AnomalySummary: AnomalySummary;
  AnomalyDto: AnomalyDto;
  DeviceDto: DeviceDto;
}

type BaseType<F> = F extends { values: readonly (infer V)[] }
  ? V
  : F extends { json: "string" }
    ? string
    : F extends { json: "number" | "integer" }
      ? number
      : F extends { json: "boolean" }
        ? boolean
        : F extends { json: "ref"; ref: infer R }
          ? R extends keyof Registry
            ? Registry[R]
            : never
          : F extends { json: "array"; items: infer I }
            ? BaseType<I>[]
            : never;

type ValueType<F> = F extends { nullable: true } ? BaseType<F> | null : BaseType<F>;

type RequiredSpecKeys<S> = {
  [K in keyof S]: S[K] extends { optional: true } ? never : K;
}[keyof S];

type OptionalSpecKeys<S> = {
  [K in keyof S]: S[K] extends { optional: true } ? K : never;
}[keyof S];

type Flatten<T> = { [K in keyof T]: T[K] } & unknown;

/** The object type a descriptor describes. */
export type Shape<S extends SchemaSpec> = Flatten<
  { [K in RequiredSpecKeys<S>]: ValueType<S[K]> } & {
    [K in OptionalSpecKeys<S>]?: ValueType<S[K]>;
  }
>;

/**
 * Type identity, not assignability.
 *
 * `{ id: number }` is assignable to `{ id?: number }`, which is exactly the
 * drift this has to catch, so the stricter relation is the one used.
 */
export type Equals<X, Y> =
  (<T>() => T extends X ? 1 : 2) extends <T>() => T extends Y ? 1 : 2 ? true : false;

/**
 * Fail the build unless the two types are identical.
 *
 * Written as a function because a type alias that merely *resolves* to an
 * error type is not an error: nothing checks it. A parameter is checked. When
 * the types differ the parameter type collapses to the diagnostic object
 * below, `true` is no longer assignable to it, and `tsc` reports the drift
 * with both types in the message.
 *
 * The returned boolean is incidental — it only lets a test read as a test.
 * The proof is the compilation, which `npm run typecheck` and `npm run build`
 * both gate on.
 */
export function assertExactType<Reconstructed, Dto>(
  proof: Equals<Reconstructed, Dto> extends true
    ? true
    : {
        drift: "the TypeScript DTO no longer matches its contract descriptor";
        reconstructed: Reconstructed;
        dto: Dto;
      },
): boolean {
  return proof === true;
}

/** Fail the build unless `Sub` is still usable everywhere `Super` is. */
export function assertAssignableType<Sub, Super>(
  proof: [Sub] extends [Super] ? true : { drift: "no longer assignable"; sub: Sub; super: Super },
): boolean {
  return proof === true;
}

// -- the runtime half --------------------------------------------------------

interface OpenApiSchema {
  properties?: Record<string, unknown>;
  required?: string[];
  additionalProperties?: boolean;
}

interface Normalised {
  json: JsonKind;
  ref?: string;
  items?: Normalised;
  nullable: boolean;
}

function unwrapNullable(schema: Record<string, unknown>): {
  inner: Record<string, unknown>;
  nullable: boolean;
} {
  const anyOf = schema.anyOf as Record<string, unknown>[] | undefined;
  if (!anyOf) return { inner: schema, nullable: false };
  const nullable = anyOf.some((entry) => entry.type === "null");
  const inner = anyOf.find((entry) => entry.type !== "null");
  if (!inner) throw new Error("anyOf carries no non-null branch");
  return { inner, nullable };
}

/** An OpenAPI property schema, reduced to the same vocabulary as a descriptor. */
export function normalise(property: unknown): Normalised {
  if (typeof property !== "object" || property === null) {
    throw new Error("property schema is not an object");
  }
  const { inner, nullable } = unwrapNullable(property as Record<string, unknown>);

  const ref = inner.$ref as string | undefined;
  if (ref) return { json: "ref", ref: ref.replace("#/components/schemas/", ""), nullable };

  const type = inner.type as string | undefined;
  if (type === "array") {
    return { json: "array", items: normalise(inner.items), nullable };
  }
  if (type === "string" || type === "number" || type === "integer" || type === "boolean") {
    return { json: type, nullable };
  }
  throw new Error(`unsupported property schema: ${JSON.stringify(inner)}`);
}

/** Field order is an artefact of how each side was written, not a difference. */
function canonical(shape: Normalised): string {
  return JSON.stringify({
    json: shape.json,
    ref: shape.ref ?? null,
    nullable: shape.nullable,
    items: shape.items ? canonical(shape.items) : null,
  });
}

function describe(spec: FieldSpec): Normalised {
  const normalised: Normalised = { json: spec.json, nullable: spec.nullable === true };
  if (spec.ref !== undefined) normalised.ref = spec.ref;
  if (spec.items !== undefined) normalised.items = describe(spec.items);
  return normalised;
}

export interface SchemaReport {
  /** Properties the backend publishes that the descriptor does not describe. */
  unexpected: string[];
  /** Properties the descriptor expects that the backend no longer publishes. */
  missing: string[];
  /** Required in the descriptor but optional in the contract, or the reverse. */
  optionalityDrift: string[];
  /** `name: expected vs published`, for anything whose shape disagrees. */
  typeDrift: string[];
  /** True when the backend refuses to send fields nobody declared. */
  closed: boolean;
}

/**
 * Compare one descriptor against the published schema.
 *
 * Returned rather than asserted, so a failing test can name every drift at
 * once instead of stopping at the first.
 */
export function checkSchema(spec: SchemaSpec, schema: OpenApiSchema | undefined): SchemaReport {
  if (!schema) throw new Error("the published contract no longer defines this schema");

  const published = new Set(Object.keys(schema.properties ?? {}));
  const required = new Set(schema.required ?? []);
  const declared = new Set(Object.keys(spec));

  const report: SchemaReport = {
    unexpected: [...published].filter((name) => !declared.has(name)),
    missing: [...declared].filter((name) => !published.has(name)),
    optionalityDrift: [],
    typeDrift: [],
    closed: schema.additionalProperties === false,
  };

  for (const [name, field] of Object.entries(spec)) {
    if (!published.has(name)) continue;
    const expectedRequired = field.optional !== true;
    if (required.has(name) !== expectedRequired) {
      report.optionalityDrift.push(
        `${name}: client expects ${expectedRequired ? "required" : "optional"}, contract says ${
          required.has(name) ? "required" : "optional"
        }`,
      );
    }
    const expected = describe(field);
    const actual = normalise(schema.properties?.[name]);
    if (canonical(expected) !== canonical(actual)) {
      report.typeDrift.push(`${name}: client expects ${canonical(expected)}, contract publishes ${canonical(actual)}`);
    }
  }

  return report;
}

/** A report with nothing to say, for a straight equality assertion. */
export const CLEAN: Omit<SchemaReport, "closed"> = {
  unexpected: [],
  missing: [],
  optionalityDrift: [],
  typeDrift: [],
};

export function findings(report: SchemaReport): Omit<SchemaReport, "closed"> {
  return {
    unexpected: report.unexpected,
    missing: report.missing,
    optionalityDrift: report.optionalityDrift,
    typeDrift: report.typeDrift,
  };
}

/**
 * Compare a descriptor against a recorded payload, for the WebSocket.
 *
 * WebSocket frames are not in `openapi.json` — the backend builds them in
 * `realtime/events.py` — so the fixture is a captured envelope of each type
 * and the check is against the values it actually carries.
 */
export function checkPayload(
  spec: SchemaSpec,
  payload: Record<string, unknown>,
): Omit<SchemaReport, "closed"> {
  const present = new Set(Object.keys(payload));
  const declared = new Set(Object.keys(spec));
  const report: Omit<SchemaReport, "closed"> = {
    unexpected: [...present].filter((name) => !declared.has(name)),
    missing: [],
    optionalityDrift: [],
    typeDrift: [],
  };

  for (const [name, field] of Object.entries(spec)) {
    if (!present.has(name)) {
      if (field.optional !== true) report.missing.push(name);
      continue;
    }
    const value = payload[name];
    if (value === null) {
      if (field.nullable !== true) report.typeDrift.push(`${name}: unexpected null`);
      continue;
    }
    const actual = Array.isArray(value) ? "array" : typeof value;
    const expected =
      field.json === "integer" || field.json === "number"
        ? "number"
        : field.json === "ref"
          ? "object"
          : field.json;
    if (actual !== expected) {
      report.typeDrift.push(`${name}: expected ${expected}, frame carries ${actual}`);
    }
    if (field.values && typeof value === "string" && !field.values.includes(value)) {
      report.typeDrift.push(`${name}: "${value}" is outside the client's union`);
    }
  }

  return report;
}
