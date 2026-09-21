/**
 * Loading, error and empty, in one place.
 *
 * Every view needs all three, and "nothing here yet" must never look like
 * "something went wrong" — on a fresh install an empty device list is the
 * expected state, not a failure.
 */

import type { ReactNode } from "react";

import type { ApiError } from "../api/errors";

export function LoadingState({ label = "Loading…" }: { label?: string }): ReactNode {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }): ReactNode {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      {hint ? <p className="state__hint">{hint}</p> : null}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: ApiError;
  onRetry?: () => void;
}): ReactNode {
  const heading =
    error.kind === "network" ? "The backend could not be reached" : "The request failed";
  return (
    <div className="state state--error" role="alert">
      <p className="state__title">{heading}</p>
      <p className="state__hint">{error.message}</p>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}
