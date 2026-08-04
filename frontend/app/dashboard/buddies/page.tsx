"use client";

import { useEffect, useState } from "react";

import { BUDDIES_API_PATH, LANDING_PATH } from "../../api";
import { useAuth } from "../../auth";
import { SiteFooter } from "../../site-footer";
import { SiteNav } from "../../site-nav";

type Buddy = {
  id: string;
  name: string;
  email: string;
  email_domain: string | null;
  phone: string | null;
  source: string;
  referral_count: number;
};

export default function BuddiesPage() {
  const auth = useAuth();

  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  return (
    <>
      <SiteNav />
      <main>
        <section className="hero" style={{ paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            {auth.status === "signed-in" ? (
              <Workspace />
            ) : auth.status === "unreachable" ? (
              <Notice heading="We could not reach the server." body="Reload the page in a moment." />
            ) : auth.status === "anonymous" ? (
              <Notice heading="Taking you back." body="You are not signed in." />
            ) : (
              <Notice heading="One moment." body="Checking your session." />
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

function Notice({ heading, body }: { heading: string; body: string }) {
  return (
    <>
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
      <p className="lede" style={{ marginTop: 18 }}>{body}</p>
    </>
  );
}

const COLUMNS = [
  { key: "name", label: "Name" },
  { key: "email", label: "Email" },
  { key: "phone", label: "Mobile" },
  { key: "email_domain", label: "Agency" },
  { key: "referral_count", label: "Referrals" },
] as const;

function Workspace() {
  const [buddies, setBuddies] = useState<Buddy[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(BUDDIES_API_PATH, { credentials: "include" });
        if (!res.ok) throw new Error();
        const data = (await res.json()) as Buddy[];
        if (!cancelled) { setBuddies(data); setError(null); }
      } catch {
        if (!cancelled) setError("We could not load your buddies just now.");
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (error) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 18 }}>{error}</p>
      </>
    );
  }

  if (buddies === null) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="body jo-note" style={{ marginTop: 18 }}>Loading your buddies.</p>
      </>
    );
  }

  if (buddies.length === 0) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="lede" style={{ marginTop: 18, maxWidth: "62ch" }}>
          No buddies yet. When an external recruiter forwards a job order into your mailbox, they
          appear here — linked to the clients they referred.
        </p>
      </>
    );
  }

  const totalReferrals = buddies.reduce((sum, b) => sum + b.referral_count, 0);

  return (
    <>
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
      <p className="lede" style={{ marginTop: 18, maxWidth: "62ch" }}>
        External recruiters who forward job orders into your mailbox. {buddies.length}{" "}
        {buddies.length === 1 ? "buddy has" : "buddies have"} referred {totalReferrals}{" "}
        {totalReferrals === 1 ? "client" : "clients"}.
      </p>

      <div className="jo-split" style={{ marginTop: 24 }}>
        <div className="cl-list">
          <p className="body jo-note" aria-live="polite">
            Showing {buddies.length} {buddies.length === 1 ? "buddy" : "buddies"}.
          </p>
          <div className="card jo-table-card">
            <table className="jo-table" style={{ tableLayout: "auto", minWidth: 720 }}>
              <thead>
                <tr>
                  {COLUMNS.map((col) => (
                    <th key={col.key} className="row-k jo-th">{col.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {buddies.map((b) => (
                  <tr key={b.id} className="jo-row">
                    <td className="jo-td jo-td-strong">{b.name}</td>
                    <td className="jo-td">{b.email}</td>
                    <td className="jo-td">
                      <PhoneCell buddy={b} />
                    </td>
                    <td className="jo-td">
                      {b.email_domain ?? <span className="muted">—</span>}
                    </td>
                    <td className="jo-td" data-nowrap="yes">{b.referral_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}

function PhoneCell({ buddy }: { buddy: Buddy }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(buddy.phone ?? "");
  const [saved, setSaved] = useState(buddy.phone ?? "");
  const [saving, setSaving] = useState(false);

  async function save() {
    const trimmed = value.trim();
    if (trimmed === saved) { setEditing(false); return; }
    setSaving(true);
    try {
      await fetch(`${BUDDIES_API_PATH}/${buddy.id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: trimmed || null }),
      });
      setSaved(trimmed || "");
      buddy.phone = trimmed || null;
      setEditing(false);
    } catch {
      /* best-effort */
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <input
        type="tel"
        className="jo-search"
        style={{ padding: "4px 8px", fontSize: "0.8125rem", width: "100%", maxWidth: 140 }}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={saving}
        autoFocus
        onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setEditing(false); }}
        onBlur={save}
      />
    );
  }

  return (
    <button
      type="button"
      className="jo-rowbtn"
      style={{ fontSize: "0.9375rem" }}
      onClick={() => { setValue(saved); setEditing(true); }}
    >
      {saved || <span className="muted">Add</span>}
    </button>
  );
}
