/**
 * The four states every remote read has, and nothing more.
 *
 * `TECH_STACK.md` rules out a server-state framework, so this is the smallest
 * thing that does the job: load, expose loading/error/empty/data, cancel on
 * unmount, and allow an explicit reload.
 *
 * Cancellation is two-layered on purpose. The request is aborted, and the
 * completion is also checked against a generation token, because an abort is a
 * request to stop rather than a guarantee of silence: a response already in
 * flight, a transport that ignores the signal, or a promise that resolved in
 * the same tick as a route change would otherwise write the previous device's
 * data into the view that replaced it.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, isAbort } from "../api/errors";

export interface ApiResource<T> {
  data: T | null;
  loading: boolean;
  error: ApiError | null;
  reload: () => void;
}

export function useApiResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { pollMs?: number } = {},
): ApiResource<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [resolvedFor, setResolvedFor] = useState<readonly unknown[] | null>(null);
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const generationRef = useRef(0);

  const { pollMs } = options;

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const current = (): boolean => generationRef.current === generation;
    const abort = new AbortController();
    const loadForThisResource = loadRef.current;

    const run = async (showSpinner: boolean): Promise<void> => {
      if (showSpinner) setLoading(true);
      try {
        const value = await loadForThisResource(abort.signal);
        if (!current()) return;
        setData(value);
        setError(null);
        setResolvedFor([...deps]);
      } catch (failure) {
        if (isAbort(failure) || !current()) return;
        setResolvedFor([...deps]);
        setError(
          failure instanceof ApiError
            ? failure
            : new ApiError("protocol", "the response could not be read"),
        );
      } finally {
        if (current()) setLoading(false);
      }
    };

    // A different device must not inherit the previous device's data while
    // its own request is pending or after its own request fails.
    setData(null);
    setError(null);
    void run(true);

    let timer: number | undefined;
    if (pollMs && pollMs > 0) {
      // A poll refreshes in place: the view must not flash back to a spinner.
      timer = window.setInterval(() => void run(false), pollMs);
    }

    return () => {
      // Invalidate before aborting: whatever arrives afterwards is stale by
      // definition, whether or not the abort was honoured.
      generationRef.current = generation + 1;
      abort.abort();
      if (timer !== undefined) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, pollMs]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const sameResource =
    resolvedFor !== null &&
    resolvedFor.length === deps.length &&
    resolvedFor.every((value, index) => Object.is(value, deps[index]));
  return {
    data: sameResource ? data : null,
    loading: sameResource ? loading : true,
    error: sameResource ? error : null,
    reload,
  };
}
