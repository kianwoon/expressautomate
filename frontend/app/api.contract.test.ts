import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import * as api from "./api";

/**
 * Every API path this app builds has to be a path the API serves.
 *
 * Nothing checked that before, and two bugs shipped through the gap: the
 * placement form PATCHed `/api/opportunities/{id}` and the panel GET the same
 * URL, and neither existed. Every test passed anyway, because a stubbed
 * `fetch` answers any URL with a plausible body — a call to a route that is
 * not there looks exactly like a call that worked, right up until production.
 *
 * The other half of this lives in `backend/tests/test_route_manifest.py`,
 * which writes `frontend/route-manifest.json` from the FastAPI app and fails
 * if the checked-in copy has drifted. So this test needs no server and no
 * Python: it reads the file, and the backend's own suite keeps the file
 * honest.
 *
 * What it cannot catch: the HTTP method. Helpers here are paths, and the verb
 * lives at the call site — a helper pointed at a real path used with a verb
 * the route does not serve still passes. Adding that would mean teaching this
 * file which caller uses which verb, which is a second copy of the thing it is
 * checking.
 */

// Resolved from the working directory, not from `import.meta.url`: the DOM
// environment these tests run in gives the module a non-`file:` URL, and
// `fileURLToPath` refuses it. Vitest runs from `frontend/`, where the manifest
// sits, and the read below throws loudly rather than skipping if that changes.
const MANIFEST: { paths: Record<string, string[]> } = JSON.parse(
  readFileSync(resolve(process.cwd(), "route-manifest.json"), "utf8"),
);

/** Placeholders for the ids helpers take. Nothing with a slash in it, so a
 *  substituted id can never look like an extra path segment. */
const ARGS = ["contract-id-1", "contract-id-2", "contract-id-3"];

/**
 * Which exports the rest of the app actually names.
 *
 * A few helpers are only ever building blocks for other helpers —
 * `candidateImportPath` exists so `…/errors` and `…/undo` can be built from
 * it, and `/api/candidates/imports/{id}` is not itself served. Nothing outside
 * `api.ts` can fetch a name it never mentions, so "referenced nowhere else" is
 * the test for a building block, and it is derived rather than listed: the day
 * somebody does call one, the reference appears and the check turns itself
 * back on.
 *
 * A prefix rule would have been the other way to excuse it, and it is exactly
 * wrong: `/api/opportunities/{id}/review` exists, so a prefix rule would have
 * waved through the missing `/api/opportunities/{id}` this file was written
 * to catch.
 */
function sourcesOutsideApiTs(): { file: string; text: string }[] {
  const root = resolve(process.cwd(), "app");
  const sources: { file: string; text: string }[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      // Tests are excluded along with `api.ts` itself: naming a helper in a
      // test — including in this file's own comments — is not the app calling
      // it, and counting it would quietly re-enable a check by writing prose.
      else if (
        /\.tsx?$/.test(entry.name) &&
        !/\.test\.tsx?$/.test(entry.name) &&
        path !== resolve(root, "api.ts")
      ) {
        sources.push({ file: path, text: readFileSync(path, "utf8") });
      }
    }
  };
  walk(root);
  return sources;
}

function namesUsedOutsideApiTs(): Set<string> {
  const text = sourcesOutsideApiTs()
    .map((s) => s.text)
    .join("\n");
  return new Set(Object.keys(api).filter((name) => text.includes(name)));
}

/** Every API path the module can produce, by the export that produced it.
 *
 * Constants are read as they are; functions are called with placeholder ids.
 * Anything that is not an `/api` path — the site routes, the mailto, the page
 * sizes, the poll interval — is not this test's business and is dropped. */
function apiPaths(): { name: string; path: string }[] {
  const used = namesUsedOutsideApiTs();
  const found: { name: string; path: string }[] = [];
  for (const [name, value] of Object.entries(api)) {
    if (!used.has(name)) continue;
    let candidate: unknown = value;
    if (typeof value === "function") {
      candidate = (value as (...args: string[]) => unknown)(
        ...ARGS.slice(0, Math.max(value.length, 1)),
      );
    }
    if (typeof candidate !== "string") continue;
    // The query string is the caller's, not the route's: `?prompt=select_account`
    // is still `/api/auth/microsoft/login`.
    const path = candidate.split("?")[0];
    if (path.startsWith("/api")) found.push({ name, path });
  }
  return found;
}

/** A concrete path matches a template when they agree segment for segment,
 *  with `{param}` standing for whatever we substituted. */
function matches(path: string, template: string): boolean {
  const actual = path.split("/");
  const expected = template.split("/");
  if (actual.length !== expected.length) return false;
  return expected.every(
    (segment, i) => (segment.startsWith("{") && segment.endsWith("}")) || segment === actual[i],
  );
}

/**
 * The other way to reach the API: a literal in a component.
 *
 * Going through a helper is the convention, not a rule the compiler enforces,
 * and a `fetch("/api/whatever")` written inline was invisible to the check
 * above — the same blind spot, reached by a different road.
 *
 * The two false positives this has to survive are handled by construction
 * rather than by a list of exceptions, which would rot the first time somebody
 * renamed a route:
 *
 *  - *A path in prose.* Comments are stripped before scanning, so a route
 *    named in a docstring or a `// TODO` is not a call. That is the only place
 *    an example path can live, so it is the only place that needs excusing.
 *  - *A path built from a variable.* `${…}` inside a template literal stands
 *    for an id, so it is substituted with the same placeholder the helpers get
 *    and matched against the route templates the same way.
 *
 * What it still cannot see, stated plainly rather than skipped quietly: a path
 * assembled from pieces that are not one literal — `` `${base}/review` `` with
 * `base` from elsewhere. Nothing short of following the value would catch
 * that, and the fragment does not start with `/api`, so it never reaches here.
 */
export function literalApiPaths(source: string): string[] {
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
  return [...code.matchAll(/["'`](\/api\/[^"'`\n]*)["'`]/g)].map(([, literal]) =>
    literal
      .replace(/\$\{[^}]*\}/g, ARGS[0])
      .split("?")[0]
      .replace(/\/$/, ""),
  );
}

function inlineApiPaths(): { where: string; path: string }[] {
  const found: { where: string; path: string }[] = [];
  for (const { file, text } of sourcesOutsideApiTs()) {
    for (const path of literalApiPaths(text)) {
      found.push({ where: `${file.split("/app/")[1]} -> ${path}`, path });
    }
  }
  return found;
}

describe("every path helper names a route the backend serves", () => {
  it("finds the helpers at all", () => {
    // Without this, a change that stopped the walk seeing anything would make
    // every assertion below pass over an empty list.
    const names = apiPaths().map((p) => p.name);
    expect(names.length).toBeGreaterThan(20);
    expect(names).toContain("opportunityPath");
    expect(Object.keys(MANIFEST.paths).length).toBeGreaterThan(20);
  });

  it.each(apiPaths())("$name -> $path", ({ path }) => {
    const template = Object.keys(MANIFEST.paths).find((t) => matches(path, t));
    expect(template, `${path} is not a route the API serves`).toBeDefined();
  });

  it("still recognises an inline path when it sees one", () => {
    // Today nothing outside `api.ts` writes one, so the list below is empty
    // and `it.each` over it would assert nothing at all — the same "passes
    // over an empty list" trap the helpers are guarded against. This checks
    // the extractor itself instead, on a source that stands in for the
    // component somebody will write next.
    const sample = [
      'await fetch("/api/candidates");',
      "await fetch(`/api/opportunities/${id}/review`);",
      '// see /api/nowhere for the shape',
      '/* GET /api/also-nowhere */',
      'href.endsWith("/client")',
    ].join("\n");
    expect(literalApiPaths(sample)).toEqual([
      "/api/candidates",
      `/api/opportunities/${ARGS[0]}/review`,
    ]);
  });

  it.each(inlineApiPaths())("$where", ({ path }) => {
    const template = Object.keys(MANIFEST.paths).find((t) => matches(path, t));
    expect(template, `${path} is not a route the API serves`).toBeDefined();
  });
});
