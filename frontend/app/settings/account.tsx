"use client";

import { useState } from "react";

import { ME_PATH } from "../api";
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
  const [name, setName] = useState(me.user.preferred_name ?? me.user.display_name ?? "");
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
      setName(nextMe.user.preferred_name ?? nextMe.user.display_name ?? "");
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
    </>
  );
}
