"use client";

import { useCallback, useEffect, useState } from "react";

import { LANDING_PATH, MAILBOX_LOOKBACK_PATH, MAILBOX_SETTINGS_PATH } from "../api";
import { useAuth } from "../auth";
import { SiteFooter } from "../site-footer";
import { SiteNav } from "../site-nav";

/**
 * "How far back do we read?", asked a second time.
 *
 * The onboarding step asks it once and the answer is usually right. This page
 * exists for the case where it was not — someone picked "last 7 days" to try
 * the product and now wants the quarter behind it.
 *
 * One direction only, and the page has to be honest about why. Moving the date
 * *later* un-imports nothing: the emails already read stay read, so a control
 * offering "last 7 days" to someone on ninety would look like a delete and
 * behave as a no-op. So only earlier periods are offered, the copy says
 * plainly that this only ever adds, and the server refuses the rest rather
 * than silently accepting them.
 *
 * The current setting is shown, not just the choices. A period picker with no
 * stated starting point asks the user to remember what they chose weeks ago.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

type Option = { key: string; label: string; days: number | null };
type Lookback = {
  initial_sync_from: string;
  backfill_complete: boolean;
  options: Option[];
};

type State =
  | { status: "loading" }
  | { status: "ready"; lookback: Lookback }
  // "Nothing to change" is not an error and must not be worded as one: a user
  // who has not finished onboarding is being told where to go, not what broke.
  | { status: "nothing-yet" }
  | { status: "unreadable"; message: string };

export default function Settings() {
  const auth = useAuth();

  // Same guard as the dashboard: only a real 401 sends you away, and it goes
  // to the landing page rather than straight into a provider redirect — the
  // choice of provider is the user's.
  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  return (
    <>
      <SiteNav />
      <main>
        <section className="hero" style={{ paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            <span className="eyebrow">Settings</span>
            <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
              How far back we read your inbox.
            </h1>
            {auth.status === "signed-in" ? (
              <LookbackSetting />
            ) : auth.status === "unreachable" ? (
              <p className="lede" style={{ marginTop: 18 }}>
                We could not reach the server. This is not a sign-in problem — your session is
                untouched. Reload the page in a moment.
              </p>
            ) : (
              /* Nothing about the mailbox before the session check resolves. */
              <p className="lede" style={{ marginTop: 18 }}>
                Checking your session.
              </p>
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

function LookbackSetting() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [chosen, setChosen] = useState<string | null>(null);
  // Distinct from the load: one means we could not read the setting, the other
  // means we read it and could not change it. They need different words, and
  // the second must not wipe the choices off the page.
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    return (async () => {
      try {
        const res = await fetch(MAILBOX_SETTINGS_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal,
        });
        if (res.status === 404) {
          setState({ status: "nothing-yet" });
          return;
        }
        if (!res.ok) {
          setState({
            status: "unreadable",
            message:
              res.status === 401
                ? "Your session has expired. Sign in again and this page will show your setting."
                : "We could not read your setting just now.",
          });
          return;
        }
        const lookback = (await res.json()) as Lookback;
        setState({ status: "ready", lookback });
        // Preselect the furthest back on offer. Unlike onboarding, where the
        // safe default is to import nothing, every option here only adds — and
        // someone who came to this page came to reach further back.
        setChosen(lookback.options[lookback.options.length - 1]?.key ?? null);
      } catch {
        if (!signal?.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function save() {
    if (!chosen || saving) return;
    setSaving(true);
    setSaveError(null);
    setSaved(null);
    try {
      const res = await fetch(MAILBOX_LOOKBACK_PATH, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ window: chosen }),
      });
      if (!res.ok) {
        // The server's own words for a refused window. It knows which period
        // is stored and this page's copy does not — paraphrasing here would
        // eventually contradict it.
        const detail = (await res.json().catch(() => null))?.detail;
        setSaveError(
          typeof detail === "string"
            ? detail
            : "We could not change your setting. Try again in a moment.",
        );
        setSaving(false);
        return;
      }
      setSaved(
        "Reading further back now. New emails appear on the dashboard as they are read — nothing already there has been removed.",
      );
      setSaving(false);
      // Re-read rather than patch the state locally: the new setting shortens
      // the list of remaining options, and computing that here would be a
      // second implementation of the rule the server owns.
      await load();
      setChosen(null);
    } catch {
      setSaveError("We could not reach the server.");
      setSaving(false);
    }
  }

  if (state.status === "loading") {
    return (
      <p className="lede" style={{ marginTop: 18 }}>
        Looking up your current setting.
      </p>
    );
  }

  if (state.status === "unreadable") {
    return (
      <p className="lede" style={{ marginTop: 18 }}>
        {state.message}
      </p>
    );
  }

  if (state.status === "nothing-yet") {
    return (
      <>
        <p className="lede" style={{ marginTop: 18 }}>
          You have not chosen a period yet, so there is nothing here to change.
        </p>
        <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
          The dashboard asks how far back to read when you connect a mailbox. Nothing is imported
          until you answer it.
        </p>
      </>
    );
  }

  const { lookback } = state;

  return (
    <>
      <p className="lede" style={{ marginTop: 18 }}>
        We are reading everything from <strong>{day(lookback.initial_sync_from)}</strong> onwards,
        and every email that arrives from now on.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
        {lookback.backfill_complete
          ? "That history has been read through."
          : "We are still working back through that history."}{" "}
        You can reach <strong>further back</strong> than this. You cannot reach less far: reading
        more history only ever <strong>adds</strong> emails, and choosing a shorter period would not
        remove any that have already been read.
      </p>

      {lookback.options.length === 0 ? (
        <p className="body" style={{ marginTop: 24, maxWidth: "62ch" }}>
          There is nothing further back to offer — this is already the earliest period we can read
          from.
        </p>
      ) : (
        <>
          <fieldset style={{ marginTop: 36, border: 0, padding: 0, margin: 0 }}>
            <legend className="eyebrow" style={{ padding: 0 }}>
              Reach further back
            </legend>
            <div
              style={{
                marginTop: 16,
                display: "grid",
                gap: 12,
                gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
              }}
            >
              {lookback.options.map((option) => (
                <label
                  key={option.key}
                  className="card"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "16px 18px",
                    cursor: "pointer",
                    // The design token, not its value: a literal here would
                    // silently stop matching the day the palette moves.
                    outline: chosen === option.key ? "2px solid var(--blue-500)" : "none",
                    outlineOffset: -1,
                  }}
                >
                  <input
                    type="radio"
                    name="lookback"
                    value={option.key}
                    checked={chosen === option.key}
                    onChange={() => setChosen(option.key)}
                  />
                  <span style={{ fontWeight: 600 }}>{option.label}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div
            style={{
              marginTop: 24,
              display: "flex",
              alignItems: "center",
              gap: 20,
              flexWrap: "wrap",
            }}
          >
            <button className="btn btn-primary" onClick={save} disabled={!chosen || saving}>
              {saving ? "Saving…" : "Read further back"}
            </button>
            <p className="body muted" style={{ margin: 0, maxWidth: "52ch", fontSize: "0.875rem" }}>
              We will walk the extra history in the background. Emails we already hold are skipped
              rather than imported twice, so nothing is duplicated.
            </p>
          </div>
        </>
      )}

      {saveError && (
        <p className="body" style={{ marginTop: 16, maxWidth: "62ch" }}>
          {saveError}
        </p>
      )}
      {saved && (
        <p className="body" style={{ marginTop: 16, maxWidth: "62ch" }}>
          {saved}
        </p>
      )}
    </>
  );
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
