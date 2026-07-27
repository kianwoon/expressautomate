"use client";

import { useCallback, useEffect, useState } from "react";

import { MAILBOX_LOOKBACK_PATH, MAILBOX_SETTINGS_PATH } from "../api";

/**
 * "How far back do we read?", asked a second time.
 *
 * Split out of the settings page when the glossary joined it there: two
 * unrelated settings in one file, each with its own fetch and its own error
 * states, is how one of them ends up sharing state with the other by accident.
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

export function LookbackSetting() {
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
        // Nothing preselected, deliberately.
        //
        // This preselected the furthest option, and a user read that filled
        // radio as a statement of their current setting — "it says 90 days,
        // but I chose a short window". They were right to: in a list of
        // *changes*, a filled radio is a claim about the present, and here it
        // was a false one. These options are extensions the user chooses, not
        // a reflection of state, so the state is stated in words above and
        // nothing is selected until someone selects it.
        setChosen(null);
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
        {/* The date alone was not enough. It is true, but it is in different
            vocabulary from the options below — someone who chose "last 7
            days" has to do the arithmetic to recognise their own setting in
            "Jul 26, 2026". The span is said in the same terms the options
            use, and said honestly: it is measured from the stored date rather
            than snapped to whichever named window is nearest, because the
            current setting need not be one of the ones on offer. */}
        Currently: <strong>{span(lookback.initial_sync_from)}</strong> of history (everything from{" "}
        <strong>{day(lookback.initial_sync_from)}</strong> onwards), and every email that arrives
        from now on.
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
            {/* Nothing is selected when the page loads, so the group needs to
                say what it is: a choice not yet made, not the setting above. */}
            <p className="body jo-sub" style={{ marginTop: 8, maxWidth: "62ch" }}>
              Nothing is selected. These are periods you can extend to — none of them is your
              current setting, and nothing changes until you pick one and press the button.
            </p>
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

/**
 * How far back the current setting actually reaches, in words.
 *
 * Approximate and says so — "about 2 days" — because the exact figure is the
 * date beside it, and this exists to be recognisable rather than precise. It
 * is never matched to one of the offered options: a setting of two days is not
 * "last 7 days", and rounding it to the nearest named window would put the
 * page back to claiming a period the user did not choose.
 */
function span(iso: string): string {
  const days = Math.round((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (!Number.isFinite(days) || days < 1) return "less than a day";
  if (days === 1) return "about a day";
  if (days < 60) return `about ${days} days`;
  const months = Math.round(days / 30);
  if (months < 24) return `about ${months} months`;
  const years = Math.round(days / 365);
  return `about ${years} years`;
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
