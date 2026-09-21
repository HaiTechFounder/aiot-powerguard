/** Failures the client can distinguish, so a view can react to each honestly. */

export type FailureKind =
  /** The backend answered with its error envelope. */
  | "api"
  /** Nothing answered: the backend is down, or the network is. */
  | "network"
  /** Something answered, but not in the shape the contract promises. */
  | "protocol";

export class ApiError extends Error {
  readonly kind: FailureKind;
  readonly status: number | null;
  readonly code: string;
  readonly details: unknown;

  constructor(
    kind: FailureKind,
    message: string,
    options: { status?: number | null; code?: string; details?: unknown } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status ?? null;
    this.code = options.code ?? kind.toUpperCase();
    this.details = options.details ?? null;
  }

  /** True when the resource is simply absent, which is often not an error. */
  get isNotFound(): boolean {
    return this.status === 404;
  }
}

/** An aborted request is a cancellation, not a failure worth showing. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
