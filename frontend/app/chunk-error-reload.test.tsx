import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChunkErrorReload } from "./chunk-error-reload";

/**
 * The component exists to notice a stale chunk — a tab left open across a
 * static-export deploy — and reload. These tests pin that contract: a webpack
 * ChunkLoadError rejection and a failed chunk script both reload, and an
 * over-eager listener must not — reloading the page for any old 404 is the
 * failure mode the sessionStorage guard exists to contain.
 *
 * `Event` subclasses like `PromiseRejectionEvent` are not constructible in
 * happy-dom, so the events are plain `Event`s with `reason`/`target` patched
 * onto them — which is all the handler reads.
 */

function dispatch(name: string, init?: { reason?: unknown; target?: EventTarget }) {
  const event = new Event(name, { bubbles: true });
  if (init?.reason !== undefined) {
    Object.defineProperty(event, "reason", { value: init.reason });
  }
  if (init?.target !== undefined) {
    Object.defineProperty(event, "target", { value: init.target });
  }
  window.dispatchEvent(event);
}

function staleChunkScript(): HTMLScriptElement {
  const script = document.createElement("script");
  script.src = "/_next/static/chunks/489-1863fec87d2f3dde.js";
  return script;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe("ChunkErrorReload", () => {
  it("reloads on a webpack ChunkLoadError rejection", () => {
    const reload = vi.spyOn(window.location, "reload").mockImplementation(() => undefined);
    render(<ChunkErrorReload />);

    dispatch("unhandledrejection", {
      reason: { name: "ChunkLoadError", message: "Loading chunk 489 failed." },
    });

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("reloads when a stale chunk script fails to load", () => {
    const reload = vi.spyOn(window.location, "reload").mockImplementation(() => undefined);
    render(<ChunkErrorReload />);

    dispatch("error", { target: staleChunkScript() });

    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("ignores unrelated rejections and resource errors", () => {
    const reload = vi.spyOn(window.location, "reload").mockImplementation(() => undefined);
    render(<ChunkErrorReload />);

    dispatch("unhandledrejection", { reason: new Error("fetch failed") });

    const image = document.createElement("img");
    image.src = "/missing.png";
    dispatch("error", { target: image });

    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads at most once per page", () => {
    const reload = vi.spyOn(window.location, "reload").mockImplementation(() => undefined);
    render(<ChunkErrorReload />);

    dispatch("unhandledrejection", { reason: { name: "ChunkLoadError", message: "Loading chunk 1 failed." } });
    dispatch("unhandledrejection", { reason: { name: "ChunkLoadError", message: "Loading chunk 2 failed." } });

    expect(reload).toHaveBeenCalledTimes(1);
  });
});
