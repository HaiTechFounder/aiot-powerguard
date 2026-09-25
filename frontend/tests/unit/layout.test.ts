/**
 * The layout rules that a screenshot caught and a unit test can keep.
 *
 * jsdom does no layout, so width cannot be measured here. What can be pinned
 * is the stylesheet contract that produced the defect: a `max-width` on the
 * main column left the right of a 1440 or 1920 viewport empty while the charts
 * stayed narrow, and a `1fr` grid track (which floors at min-content) is what
 * pushes a wide chart past the viewport edge. Both are one-line regressions,
 * so both are asserted.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// The jsdom environment does not give `import.meta.url` a file:// scheme, so
// the stylesheet is resolved from the project root Vitest already runs in.
const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

/** The body of the first rule whose selector matches exactly. */
function rule(selector: string): string {
  const match = new RegExp(`(?:^|\\})\\s*${selector.replace(".", "\\.")}\\s*\\{([^}]*)\\}`, "m").exec(
    css,
  );
  if (!match?.[1]) throw new Error(`no rule found for ${selector}`);
  return match[1];
}

describe("the desktop layout", () => {
  it("lets the main column fill the viewport instead of capping it", () => {
    expect(rule(".main")).not.toMatch(/max-width/);
  });

  it("uses a fluid main track that cannot overflow, beside a fixed rail", () => {
    const dashboard = rule(".dashboard");
    expect(dashboard).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s+2[6-8]0px/);
  });

  it("keeps the sidebar at the agreed width", () => {
    expect(rule(".sidebar")).toMatch(/width:\s*220px/);
  });

  it("spreads the four KPI cards evenly across the row", () => {
    expect(rule(".metric-row")).toMatch(/repeat\(4,\s*minmax\(0,\s*1fr\)\)/);
  });

  it("keeps the three trend charts on one aligned row", () => {
    expect(rule(".chart-grid")).toMatch(/repeat\(3,\s*minmax\(0,\s*1fr\)\)/);
  });

  it("clamps the page against horizontal overflow at any width", () => {
    expect(css).toMatch(/html,\s*body\s*\{[^}]*overflow-x:\s*hidden/);
  });

  it("still collapses the rail and the KPI row on narrow viewports", () => {
    expect(css).toMatch(/@media \(max-width: 1140px\)/);
    expect(css).toMatch(/@media \(max-width: 430px\)/);
  });
});
