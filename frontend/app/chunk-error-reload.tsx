"use client";

import { useEffect } from "react";

/**
 * A static-export deploy replaces every chunk with a new content-hashed name,
 * so a tab left open across a deploy is still running the old bundle. The next
 * navigation requests a chunk the server no longer has — a 404 that surfaces
 * as a `ChunkLoadError` and leaves the page broken with nothing to click.
 *
 * There is nothing to fix on the server: the old file is gone by design, and
 * serving every historical chunk forever is what a CDN cache is for, not the
 * app. The fix is to notice and reload, which fetches the current HTML and the
 * bundle that goes with it. Renders nothing — it exists to run one effect.
 */
export function ChunkErrorReload() {
  useEffect(() => {
    // Webpack's dynamic `import()` of a missing chunk rejects with a
    // ChunkLoadError, which nothing in React catches — it surfaces as an
    // unhandled promise rejection. That is the primary path.
    const isChunkFailure = (reason: unknown): boolean => {
      if (typeof reason !== "object" || reason === null) return false;
      const err = reason as { name?: unknown; message?: unknown };
      if (err.name === "ChunkLoadError") return true;
      const message = typeof err.message === "string" ? err.message : "";
      return (
        message.includes("ChunkLoadError") ||
        message.includes("Loading chunk") ||
        message.includes("Loading CSS chunk")
      );
    };

    let reloading = false;
    const reload = () => {
      if (reloading) return;
      // The flag covers repeats within this page; sessionStorage covers a
      // reload that lands on a server still failing — do not reload-loop a
      // genuinely broken deploy faster than once every five seconds per tab.
      const last = Number(sessionStorage.getItem("chunk-error-reload-at") ?? 0);
      if (Date.now() - last < 5_000) return;
      sessionStorage.setItem("chunk-error-reload-at", String(Date.now()));
      reloading = true;
      window.location.reload();
    };

    const onRejection = (event: PromiseRejectionEvent) => {
      if (isChunkFailure(event.reason)) reload();
    };

    // A `<script>` or stylesheet `<link>` pointing at a missing chunk fires an
    // error event whose target is that element — the same stale-bundle story,
    // told by the resource loader instead of by webpack.
    const onError = (event: Event) => {
      const target = event.target;
      if (target instanceof HTMLScriptElement && target.src.includes("/_next/static/")) reload();
      if (target instanceof HTMLLinkElement && target.href.includes("/_next/static/")) reload();
    };

    window.addEventListener("unhandledrejection", onRejection);
    window.addEventListener("error", onError);
    return () => {
      window.removeEventListener("unhandledrejection", onRejection);
      window.removeEventListener("error", onError);
    };
  }, []);

  return null;
}
