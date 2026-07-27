"use client";

import { useEffect } from "react";

import { LANDING_PATH, SWITCH_ACCOUNT_PATH } from "../api";
import { displayNameOf, useAuth, type Me } from "../auth";
import { SiteNav } from "../site-nav";

/**
 * The signed-in shell. Ingestion and AI extraction do not exist yet, so this
 * page deliberately shows no job data and no counts — there is nothing true to
 * put there, and an invented number would be the one thing the product
 * promises not to do.
 */
export default function Dashboard() {
  const auth = useAuth();

  // Guard. Only a real 401 sends you away, and it goes to the landing page —
  // the user's own choice of provider belongs to them, so we never bounce
  // straight into the sign-in redirect. "unreachable" stays put and says so.
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
              <SignedIn me={auth.me} />
            ) : auth.status === "unreachable" ? (
              <Notice
                eyebrow="Connection"
                heading="We could not reach the server."
                body="This is not a sign-in problem — your session is untouched. Reload the page in a moment. If it keeps failing, the service is down and we are looking at it."
              />
            ) : auth.status === "anonymous" ? (
              <Notice
                eyebrow="Signed out"
                heading="Taking you back."
                body="You are not signed in. Returning to the home page."
              />
            ) : (
              /* Nothing sensitive before the check resolves: no name, no
                 tenant, no mailbox state — only that we are checking. */
              <Notice eyebrow="Checking" heading="One moment." body="Checking your session." />
            )}
          </div>
        </section>
      </main>
    </>
  );
}

function Notice({ eyebrow, heading, body }: { eyebrow: string; heading: string; body: string }) {
  return (
    <>
      <span className="eyebrow">{eyebrow}</span>
      <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
      <p className="lede" style={{ marginTop: 18 }}>
        {body}
      </p>
    </>
  );
}

function SignedIn({ me }: { me: Me }) {
  const personal = me.tenant.is_personal_account;
  const connected = me.mailbox.connected;

  return (
    <>
      <span className="eyebrow">Your account</span>
      <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
        Signed in as{" "}
        <span className="gradient-text">{displayNameOf(me)}</span>
      </h1>
      <p className="lede" style={{ marginTop: 18 }}>
        Setup is done. There is nothing to look at yet — ingestion is not switched on, so no
        email has been read and no job records exist.
      </p>

      <div className="grid-3" style={{ marginTop: 40 }}>
        <div className="card">
          <h3>Account</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <Row k="Name" v={me.user.display_name?.trim() || null} />
            <Row k="Email" v={me.user.email} />
            <Row k="Role" v={me.user.role} />
          </div>
        </div>

        <div className="card">
          <h3>Agency</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <Row k="Tenant" v={me.tenant.name} />
            <Row k="Type" v={personal ? "Personal Microsoft account" : "Work or school account"} />
          </div>
          <p className="body" style={{ marginTop: 14, fontSize: "0.875rem" }}>
            {personal
              ? "This is your own private workspace. Colleagues signing in with the same personal account type do not join it."
              : "Colleagues who sign in with the same work account share this agency and its data."}
          </p>
        </div>

        <div className="card">
          <h3>Mailbox</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <Row k="Provider" v={me.mailbox.provider} />
            <Row k="Connected" v={connected ? "Yes" : "No"} />
            <Row k="Ingestion" v={me.mailbox.ingestion_active ? "Running" : "Not running"} />
          </div>
          <p className="body" style={{ marginTop: 14, fontSize: "0.875rem" }}>
            {connected
              ? "We hold read-only access to this mailbox. Nothing is being read from it yet."
              : "No mailbox is connected, so there is nothing for us to read."}
          </p>
        </div>
      </div>

      {personal && (
        <div className="card" style={{ marginTop: 28, maxWidth: "72ch" }}>
          <h3>A personal account has no agency mailbox behind it</h3>
          <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
            You signed in with a personal Microsoft account, so you can use the site, but there is
            no shared agency mailbox to ingest — the recruitment mail this product reads arrives in
            a work or school Microsoft 365 mailbox. To set an agency up, sign in with your work
            account instead. We would rather say this than show you an empty dashboard and let you
            wonder.
          </p>
          {/* The paragraph above tells them to sign in with their work
              account; without this button that instruction is unfollowable.
              Microsoft would reuse this personal session and hand the same
              account straight back, so the picker has to be forced. */}
          <a
            className="btn btn-primary"
            rel="nofollow"
            href={SWITCH_ACCOUNT_PATH}
            style={{ marginTop: 18 }}
          >
            Sign in with a work account
          </a>
        </div>
      )}

      <div style={{ marginTop: 40, maxWidth: "72ch" }}>
        <span className="eyebrow">What happens next</span>
        <h2 style={{ marginTop: 12 }}>Honestly: not much, yet.</h2>
        <ol className="steps" style={{ marginTop: 24 }}>
          {[
            "Mailbox ingestion is not live. No email has been read, and none will be until we switch it on for your agency.",
            "AI extraction is not live either. There are no job records, no review queue and no export — not empty ones, none at all.",
            "When ingestion is ready we will ask you to choose how far back to start, and this page will begin showing what was actually read.",
          ].map((s, i) => (
            <li className="step" key={s}>
              <span className="step-n">{i + 1}</span>
              <p className="body" style={{ fontSize: "0.9375rem" }}>
                {s}
              </p>
            </li>
          ))}
        </ol>
      </div>
    </>
  );
}

function Row({ k, v }: { k: string; v: string | null }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>{v ?? "Not mentioned"}</span>
    </div>
  );
}
