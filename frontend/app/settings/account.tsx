"use client";

import { useCallback, useState } from "react";

import { ME_PATH, USER_EMAILS_API_PATH } from "../api";
import { useAuth, type Me } from "../auth";

/**
 * The name a candidate sees in a WhatsApp message.
 *
 * Split out rather than folded into `account-details.tsx`: that component is
 * read-only reference material folded under a `<details>`, and this is a form
 * with save states — mixing the two would make the fold-away card stateful for
 * a reason unrelated to why it exists. Read-only fields below reuse its
 * `Row`-shaped layout by hand rather than importing it, since `Row` is not
 * exported and duplicating two `<span>`s is cheaper than widening that
 * component's surface for one caller.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "error"; message: string };

export function AccountSetting() {
  const auth = useAuth();

  if (auth.status !== "signed-in") {
    return (
      <p className="lede" style={{ marginTop: 18 }}>
        Looking up your account.
      </p>
    );
  }

  return <AccountForm me={auth.me} />;
}

function AccountForm({ me }: { me: Me }) {
  // Only the chosen name, never the provider's. Pre-filling with
  // `display_name` meant that opening this page and pressing Save copied
  // Microsoft's name into `preferred_name` and quietly stopped it ever
  // updating again — the user would have had no way to know they had opted
  // out of something. Empty means "no choice made", which is exactly what the
  // placeholder below says, and saving it empty keeps the provider's name.
  const [name, setName] = useState(me.user.preferred_name ?? "");
  const [save, setSave] = useState<SaveState>({ status: "idle" });
  const [savedOnce, setSavedOnce] = useState(false);

  async function submit(value: string | null) {
    if (save.status === "saving") return;
    setSave({ status: "saving" });
    try {
      const res = await fetch(ME_PATH, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ preferred_name: value }),
      });
      if (!res.ok) {
        // The server's own words for a refused name — it knows the length and
        // newline rules and this page does not, so paraphrasing here would
        // eventually contradict it.
        const detail = (await res.json().catch(() => null))?.detail;
        setSave({
          status: "error",
          message: typeof detail === "string" ? detail : "We could not save your name just now.",
        });
        return;
      }
      const nextMe = (await res.json()) as Me;
      setName(nextMe.user.preferred_name ?? "");
      setSave({ status: "idle" });
      setSavedOnce(true);
      // `useAuth` fetches once per mount with no shared cache and no push
      // channel for a profile change (the live stream only carries mail,
      // extraction and mailbox nudges — see `events.ts`). A reload is the
      // least invasive way to get the nav and dashboard, which each hold
      // their own `useAuth`, to read the name that was just saved rather than
      // the one they fetched on mount.
      window.location.reload();
    } catch {
      setSave({ status: "error", message: "We could not reach the server." });
    }
  }

  const trimmed = name.trim();
  const hasPreferredName = !!me.user.preferred_name;

  return (
    <>
      <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
        This is the name a candidate sees when you WhatsApp them.
      </p>

      <div className="jo-panels" style={{ marginTop: 24 }}>
        <div className="card">
          <h3>Your name</h3>
          <label style={{ display: "block", marginTop: 12 }}>
            <span className="row-k">Name</span>
            <input
              type="text"
              className="jo-search"
              style={{ marginTop: 6, width: "100%" }}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={save.status === "saving"}
              // What is being used right now while the box is empty, so the
              // field can stay empty — meaning "keep following the provider"
              // — without looking as though no name exists at all.
              placeholder={me.user.display_name?.trim() || "Your name"}
            />
          </label>

          <div
            style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}
          >
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => submit(trimmed || null)}
              disabled={save.status === "saving" || trimmed === ""}
            >
              {save.status === "saving" ? "Saving…" : "Save"}
            </button>
            {hasPreferredName && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => submit(null)}
                disabled={save.status === "saving"}
              >
                Use the name from {me.mailbox.provider === "google" ? "Google" : "Microsoft"} instead
              </button>
            )}
          </div>

          {save.status === "error" && (
            <p className="body jo-detail-error" role="alert" style={{ marginTop: 12 }}>
              {save.message}
            </p>
          )}
          {savedOnce && save.status === "idle" && (
            <p className="body" style={{ marginTop: 12 }}>
              Saved.
            </p>
          )}
        </div>

        <div className="card">
          <h3>Account</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <div className="row">
              <span className="row-k">Email</span>
              <span>{me.user.email}</span>
            </div>
            <div className="row">
              <span className="row-k">Role</span>
              <span>{me.user.role}</span>
            </div>
          </div>
        </div>
      </div>

      <div className="jo-panels" style={{ marginTop: 24 }}>
        <EmailAliases />
      </div>
    </>
  );
}

type Alias = { id: string; email: string; verified: boolean };

function EmailAliases() {
  const [aliases, setAliases] = useState<Alias[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch(USER_EMAILS_API_PATH, { credentials: "include" });
      if (res.ok) setAliases((await res.json()) as Alias[]);
    } catch {
      /* not critical — the list is best-effort */
    }
    setLoaded(true);
  }, []);

  if (!loaded) {
    void load();
    return null;
  }

  async function addAlias() {
    const email = input.trim().toLowerCase();
    if (!email || adding) return;
    setAdding(true);
    setError(null);
    try {
      const res = await fetch(USER_EMAILS_API_PATH, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!res.ok) {
        const detail = (await res.json().catch(() => null))?.detail;
        setError(typeof detail === "string" ? detail : "Could not add that email.");
        return;
      }
      const row = (await res.json()) as Alias;
      setAliases((prev) => [...prev, row]);
      setInput("");
    } catch {
      setError("We could not reach the server.");
    } finally {
      setAdding(false);
    }
  }

  async function removeAlias(id: string) {
    try {
      await fetch(`${USER_EMAILS_API_PATH}/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      setAliases((prev) => prev.filter((a) => a.id !== id));
    } catch {
      /* best-effort */
    }
  }

  return (
    <div className="card">
      <h3>Email aliases</h3>
      <p className="body" style={{ marginTop: 8, maxWidth: "52ch" }}>
        Your other email addresses — typically your work email at a partner agency. The system
        recognises these as yours, so forwarded emails from your own address are not mistaken for a
        buddy.
      </p>

      {aliases.length > 0 && (
        <div className="rows" style={{ marginTop: 16 }}>
          {aliases.map((a) => (
            <div className="row" key={a.id}>
              <span className="row-k">{a.email}</span>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => removeAlias(a.id)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      <label style={{ display: "block", marginTop: 16 }}>
        <span className="row-k">Add an email</span>
        <input
          type="email"
          className="jo-search"
          style={{ marginTop: 6, width: "100%" }}
          placeholder="work@agency.com"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={adding}
          onKeyDown={(e) => {
            if (e.key === "Enter") addAlias();
          }}
        />
      </label>

      <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <button
          type="button"
          className="btn btn-primary"
          onClick={addAlias}
          disabled={adding || !input.trim()}
        >
          {adding ? "Adding…" : "Add"}
        </button>
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 12 }}>
          {error}
        </p>
      )}
    </div>
  );
}
