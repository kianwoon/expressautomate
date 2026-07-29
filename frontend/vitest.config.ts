import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The repo's other Node package, `gateway/`, runs its tests with
// `node --test` + `tsx` — no framework, no DOM. That works there because the
// gateway is a Fastify service: its tests exercise plain functions and HTTP
// handlers, nothing ever renders.
//
// `frontend/` is different in a way that isn't optional: these are React 19
// hooks and components, and pinning `useCandidates`'s behaviour (§ the
// candidates-list stickiness bug) means rendering it with
// `@testing-library/react` and asserting on what it returns across
// re-renders. `node --test` has no DOM and no renderer, so it cannot do
// this. Vitest is the same idea as `node --test` — fast, native ESM, no
// separate runner process — with a DOM (`happy-dom`, chosen over `jsdom` for
// speed; swap if it ever fights a React 19 API) and first-class
// `@testing-library/react` support layered on. If a future test here needs
// no DOM, prefer a plain `node --test` file in that case rather than
// reaching for Vitest out of habit — the split should track "does this test
// render", not "which package it lives in".
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "happy-dom",
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", "out", ".next"],
  },
});
