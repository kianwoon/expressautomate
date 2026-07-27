"use client";

import { useCallback, useEffect, useState } from "react";

import { GLOSSARY_PATH, glossaryEntryPath } from "../api";

/**
 * The one place that talks to the glossary endpoint.
 *
 * Kept apart from the components so the fetch states — loading, empty, and
 * unreadable — stay three distinct things. A failed request must never render
 * as "you have no codes": that would tell an agency their glossary is empty
 * when in fact we could not read it, and the obvious next move, adding the
 * codes again, would collide with the ones already there.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against. `attributes` in particular is never hardcoded — it is
 * the server's vocabulary and arrives with every read.
 */

export type CodeSource = "starter" | "agency";

export type GlossaryCode = {
  id: string;
  code: string;
  meaning: string;
  /** Which protected characteristic the code refers to, or null for none.
   *  Always one of the `attributes` the same response carried. */
  attribute: string | null;
  source: CodeSource;
  notes: string | null;
};

type Glossary = { codes: GlossaryCode[]; attributes: string[] };

export type GlossaryState =
  | { status: "loading" }
  | { status: "ready"; codes: GlossaryCode[]; attributes: string[] }
  | { status: "unreadable"; message: string };

/** What the caller sends when adding or editing. `code` is fixed once created —
 *  the code is what appeared in the email, so changing it would re-point every
 *  past decoding at a string the sender never wrote. */
export type CodeDraft = {
  code?: string;
  meaning: string;
  attribute: string | null;
  notes: string | null;
};

/** A refused write, in words. `conflict` carries the meaning already stored
 *  under that code, which is the only thing that makes a 409 actionable. */
export type WriteError = { message: string; conflict?: boolean };

function readMessage(status: number): string {
  return status === 401
    ? "Your session has expired. Sign in again to see your glossary."
    : "We could not load your glossary just now.";
}

async function detailOf(res: Response): Promise<string | null> {
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === "string" ? body.detail : null;
}

async function writeError(res: Response, verb: string): Promise<WriteError> {
  // The 409 is the case this whole path exists for. The server knows which
  // meaning is already stored under the code and this page does not, so its
  // words are used rather than paraphrased — a generic "that failed" here
  // would leave someone retyping a code that is already defined.
  if (res.status === 409) {
    const detail = await detailOf(res);
    return {
      conflict: true,
      message:
        detail ??
        "That code is already in your glossary. Edit the existing entry rather than adding a second one.",
    };
  }
  if (res.status === 401) {
    return { message: `Your session has expired. Sign in again, then ${verb}.` };
  }
  const detail = await detailOf(res);
  return { message: detail ?? `We could not ${verb}. Nothing has changed.` };
}

export type GlossaryApi = {
  state: GlossaryState;
  add: (draft: CodeDraft) => Promise<WriteError | null>;
  edit: (id: string, draft: CodeDraft) => Promise<WriteError | null>;
  remove: (id: string) => Promise<WriteError | null>;
};

export function useGlossary(enabled: boolean): GlossaryApi {
  const [state, setState] = useState<GlossaryState>({ status: "loading" });

  const load = useCallback((signal?: AbortSignal) => {
    return (async () => {
      try {
        const res = await fetch(GLOSSARY_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal,
        });
        if (!res.ok) {
          setState({ status: "unreadable", message: readMessage(res.status) });
          return;
        }
        const body = (await res.json()) as Glossary;
        setState({
          status: "ready",
          codes: body.codes ?? [],
          attributes: body.attributes ?? [],
        });
      } catch {
        // An aborted fetch is this section unmounting, not a failure. Left in
        // "loading": there is nobody left to tell.
        if (!signal?.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [enabled, load]);

  // Every write re-reads the list rather than patching it locally. Editing a
  // starter row promotes it to the agency's own, and that promotion is the
  // server's rule — reimplementing it here would eventually disagree with it.
  const write = useCallback(
    async (
      url: string,
      method: string,
      verb: string,
      draft?: CodeDraft,
    ): Promise<WriteError | null> => {
      try {
        const res = await fetch(url, {
          method,
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: draft ? JSON.stringify(draft) : undefined,
        });
        if (!res.ok) return await writeError(res, verb);
        await load();
        return null;
      } catch {
        return { message: "We could not reach the server. Nothing has changed." };
      }
    },
    [load],
  );

  const add = useCallback(
    (draft: CodeDraft) => write(GLOSSARY_PATH, "POST", "add that code", draft),
    [write],
  );
  const edit = useCallback(
    (id: string, draft: CodeDraft) =>
      write(glossaryEntryPath(id), "PATCH", "save that change", draft),
    [write],
  );
  const remove = useCallback(
    (id: string) => write(glossaryEntryPath(id), "DELETE", "delete that code"),
    [write],
  );

  return { state, add, edit, remove };
}

/** `marital_status` reads as a column name. This is the only transformation —
 *  the values themselves come from the server, never from a list here. */
export function attributeLabel(attribute: string): string {
  const spaced = attribute.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
