"use client";

import { Breakable } from "../breakable";
import type { Me } from "../auth";

/**
 * Setup facts, folded away.
 *
 * Not deleted: when something misbehaves these are the first things anyone
 * needs, and "where do I see which account this is?" is a real question. But
 * they are reference material — read once, then noise — so they cost a click
 * rather than a third of the page.
 *
 * `<details>` rather than a state hook deliberately: it works before hydration
 * and is what a browser already knows how to open, close and find-in-page.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */
export function AccountDetails({ me }: { me: Me }) {
  return (
    <details style={{ marginTop: 48 }}>
      <summary
        className="eyebrow"
        style={{ cursor: "pointer", listStyle: "revert", padding: "4px 0" }}
      >
        Account and workspace details
      </summary>

      {/* Two cards, not three. The mailbox card that used to sit here is now
          the Mailbox overview in the workspace above, where it is visible
          without a click — it is the one of the three that goes wrong, so it
          was the wrong one to fold away. */}
      <div className="jo-panels" style={{ marginTop: 20 }}>
        <div className="card">
          <h3>Account</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <Row k="Name" v={me.user.display_name?.trim() || null} />
            <Row k="Email" v={me.user.email} />
            <Row k="Role" v={me.user.role} />
          </div>
        </div>

        <div className="card">
          <h3>Workspace</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <Row k="Name" v={me.tenant.name} />
            <Row
              k="Account type"
              v={me.tenant.is_personal_account ? "Personal Microsoft" : "Work or school"}
            />
          </div>
          <p className="body" style={{ marginTop: 14, fontSize: "0.875rem" }}>
            {me.tenant.is_personal_account
              ? "A workspace of one. Personal Microsoft accounts each get their own, so nobody else can join this one — a work account is what lets colleagues share an agency."
              : "Colleagues who sign in with the same work account share this agency and its data."}
          </p>
        </div>
      </div>
    </details>
  );
}

/**
 * `empty` is deliberately per-row rather than one shared fallback.
 *
 * "Not mentioned" is extraction vocabulary — it means the AI read the email and
 * the field was not in it (§15). Reusing it for an account field that is simply
 * unset says something quite different and slightly wrong.
 */
function Row({ k, v, empty = "Not mentioned" }: { k: string; v: string | null; empty?: string }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>{v ? <Breakable text={v} /> : empty}</span>
    </div>
  );
}
