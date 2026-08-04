"use client";

import { useEffect, useState } from "react";

import { BUDDIES_API_PATH, LANDING_PATH } from "../../api";
import { useAuth } from "../../auth";
import { SiteFooter } from "../../site-footer";
import { SiteNav } from "../../site-nav";

/**
 * The buddy network — external recruiters who forward job orders into your
 * mailbox. Each buddy is linked to the clients they referred.
 */

type Buddy = {
  id: string;
  name: string;
  email: string;
  email_domain: string | null;
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
              <Notice
                heading="We could not reach the server."
                body="Reload the page in a moment."
              />
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
      <p className="lede" style={{ marginTop: 18 }}>
        {body}
      </p>
    </>
  );
}

const COLUMNS = [
  { key: "name", label: "Name" },
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
        if (!cancelled) setBuddies(data);
      } catch {
        if (!cancelled) setError("We could not load your buddies just now.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 18 }}>
          {error}
        </p>
      </>
    );
  }

  if (buddies === null) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="body jo-note" style={{ marginTop: 18 }}>
          Loading your buddies.
        </p>
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
            <table className="jo-table jo-table-clients">
              <colgroup>
                <col style={{ width: "35%" }} />
                <col style={{ width: "40%" }} />
                <col style={{ width: "25%" }} />
              </colgroup>
              <thead>
                <tr>
                  {COLUMNS.map((column) => (
                    <th key={column.key} className="row-k jo-th">
                      {column.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {buddies.map((b) => (
                  <tr key={b.id} className="jo-row">
                    <td className="jo-td jo-td-strong">{b.name}</td>
                    <td className="jo-td">
                      {b.email_domain ?? <span className="muted">Not mentioned</span>}
                    </td>
                    <td className="jo-td" data-nowrap="yes">
                      {b.referral_count}
                    </td>
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
