import "@testing-library/jest-dom/vitest";

/**
 * A test-environment wart, isolated here rather than worked around in the app.
 *
 * jsdom installs its own `AbortController`, and the undici copy that MSW's
 * fetch interceptor validates against rejects that controller's signal as a
 * foreign object. In a real browser there is one realm and the question never
 * arises, so rather than weakening the client's cancellation the signal is
 * dropped at the boundary here. The hooks still ignore a late result, which is
 * the behaviour the tests are actually about.
 *
 * The property is redefined rather than assigned because MSW replaces
 * `globalThis.fetch` when its server starts, well after this file runs.
 */
let currentFetch = globalThis.fetch;
Object.defineProperty(globalThis, "fetch", {
  configurable: true,
  get() {
    return (input: RequestInfo | URL, init?: RequestInit) =>
      currentFetch(input, init ? { ...init, signal: undefined } : init);
  },
  set(next: typeof fetch) {
    currentFetch = next;
  },
});

// jsdom installs its own AbortController, while `fetch` in this environment is
// Node's. Handing a jsdom signal to Node's fetch is rejected as a cross-realm
// object — a mismatch that exists only here, because in a real browser both
// come from the same realm. Using Node's implementation keeps the tests
// faithful to the browser rather than to jsdom's patchwork.
const nodeAbortController = globalThis.constructor.constructor("return AbortController")();
globalThis.AbortController = nodeAbortController;
globalThis.AbortSignal = nodeAbortController.prototype.constructor.name
  ? globalThis.AbortSignal
  : globalThis.AbortSignal;

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// Recharts measures its container; jsdom reports zero, which would render an
// empty chart. A fixed size keeps the chart assertions meaningful.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
  configurable: true,
  value: 800,
});
Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
  configurable: true,
  value: 300,
});
