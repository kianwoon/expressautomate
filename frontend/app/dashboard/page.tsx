"use client";

import { useEffect } from "react";

import { CONNECT_MAILBOX_PATH, LANDING_PATH, SWITCH_ACCOUNT_PATH } from "../api";
import { displayNameOf, useAuth, type Me } from "../auth";
import { SiteNav } from "../site-nav";

/**
 * The signed-in shell.
 *
 * Ingestion is live, so this page reports what actually happened rather than
 * explaining that nothing has. Every number on it is counted from stored rows;
 * none is estimated, and where there is nothing to say it says so.
 *
 * Organised around one question — is this mailbox reading mail? — because that
 * is the only thing a recruiter opening it wants to know, and the three
 * answers need three different actions.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
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

/** The states a mailbox can be in, and what each needs from the user. */
type Stage = "none" | "reconnect" | "starting" | "ingesting";

function stageOf(me: Me): Stage {
  // `needs_reauth` first: the grant is still on file, so `connected` stays
  // true while nothing is being read. Checking connection first would show
  // someone a healthy dashboard for a mailbox that stopped hours ago.
  if (me.mailbox.status === "needs_reauth") return "reconnect";
  if (me.mailbox.ingestion_active) return "ingesting";
  // Consent landed, subscription not created yet — a real state that lasts
  // seconds. Without it this fell through to "none" and offered Connect to
  // someone who had just connected, inviting them to do it twice.
  if (me.mailbox.status === "active") return "starting";
  return "none";
}

function SignedIn({ me }: { me: Me }) {
  const stage = stageOf(me);

  return (
    <>
      <span className="eyebrow">Your account</span>
      <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
        Signed in as <span className="gradient-text">{displayNameOf(me)}</span>
      </h1>

      {stage === "ingesting" ? (
        <Ingesting me={me} />
      ) : stage === "reconnect" ? (
        <Reconnect />
      ) : stage === "starting" ? (
        <Starting />
      ) : (
        <NotConnected me={me} />
      )}

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

        <div className="card">
          <h3>Mailbox</h3>
          <div className="rows" style={{ marginTop: 12 }}>
            <Row k="Permission" v={me.mailbox.connected ? "Granted" : "Not granted"} />
            <Row k="State" v={stateLabel(me)} />
            <Row k="Last activity" v={when(me.mailbox.last_activity)} empty="Nothing yet" />
          </div>
          <p className="body" style={{ marginTop: 14, fontSize: "0.875rem" }}>
            Read-only. We never send, reply to, or delete anything.
          </p>
        </div>
      </div>

      <Extraction extracted={me.mailbox.ingested.extracted} />
    </>
  );
}

function Ingesting({ me }: { me: Me }) {
  const { total, in_progress: inProgress, extracted } = me.mailbox.ingested;

  return (
    <>
      <p className="lede" style={{ marginTop: 18 }}>
        {/* No delivery-time promise: Microsoft decides when a notification
            arrives, and a stated "minute or two" would be our claim to keep. */}
        {total === 0
          ? "Your mailbox is connected and being watched. Nothing has arrived yet — new mail appears here shortly after it reaches Outlook."
          : `We have read ${plural(total, "email")} from your mailbox.`}
      </p>

      {total > 0 && (
        <div className="grid-3" style={{ marginTop: 32 }}>
          <Stat n={total} label="emails read" sub={range(me)} />
          <Stat
            n={inProgress}
            label="still processing"
            sub={inProgress === 0 ? "Nothing queued" : "Usually seconds each"}
          />
          <Stat n={extracted} label="job orders found" sub="Extraction is not live yet" />
        </div>
      )}
    </>
  );
}

function Starting() {
  return (
    <>
      <p className="lede" style={{ marginTop: 18 }}>
        Your mailbox is connected. We are setting up the watch on it now — this takes a few
        seconds, and mail starts arriving here once it is done.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
        Nothing to do. Refresh in a moment.
      </p>
    </>
  );
}

function Reconnect() {
  return (
    <>
      <p className="lede" style={{ marginTop: 18 }}>
        {/* "Picks up where it left off" only holds while the delta checkpoint
            is still valid; after a long enough outage Graph expires it and the
            walk restarts. Promising continuity outright would be a claim we
            cannot keep for the case that matters most. */}
        Microsoft has stopped letting us read this mailbox, so ingestion has paused. Nothing
        already read has been lost, and reconnecting resumes it.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
        This usually means the permission was revoked, a password changed, or the grant simply
        aged out.
      </p>
      <a
        className="btn btn-primary"
        rel="nofollow"
        style={{ marginTop: 20, display: "inline-block" }}
        href={CONNECT_MAILBOX_PATH}
      >
        Reconnect your mailbox
      </a>
    </>
  );
}

function NotConnected({ me }: { me: Me }) {
  return (
    <>
      <p className="lede" style={{ marginTop: 18 }}>
        Your account is set up. Connect a mailbox and we will start reading the recruitment mail
        that arrives in it.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
        Microsoft will ask you to approve read-only access. Some organisations require an
        administrator to approve it — if that happens the request goes to them, and signing in is
        unaffected either way.
      </p>
      <div style={{ marginTop: 20, display: "flex", gap: 12, flexWrap: "wrap" }}>
        <a className="btn btn-primary" rel="nofollow" href={CONNECT_MAILBOX_PATH}>
          Connect your mailbox
        </a>
        {/* Offered to everyone, not only personal accounts: the account someone
            signs in with and the mailbox they want read are often different,
            and Microsoft reuses the browser session unless the picker is
            forced. */}
        <a className="btn" rel="nofollow" href={SWITCH_ACCOUNT_PATH}>
          Use a different account
        </a>
      </div>
      {me.tenant.is_personal_account && (
        <p className="body muted" style={{ marginTop: 16, maxWidth: "62ch", fontSize: "0.875rem" }}>
          You are on a personal Microsoft account. Its mailbox connects like any other — this is
          simply a workspace of one, so colleagues cannot join it.
        </p>
      )}
    </>
  );
}

function Extraction({ extracted }: { extracted: number }) {
  return (
    <div style={{ marginTop: 40, maxWidth: "72ch" }}>
      <span className="eyebrow">What is not live yet</span>
      <h2 style={{ marginTop: 12 }}>Reading, not yet understanding.</h2>
      <p className="body" style={{ marginTop: 16 }}>
        Mail is collected and stored. Turning it into structured job orders — company, role,
        salary, hours, location — is the next piece and is not switched on, which is why{" "}
        <strong>job orders found</strong> is {extracted}. That is a real count rather than a
        placeholder: nothing has been extracted because nothing extracts yet.
      </p>
      <p className="body" style={{ marginTop: 12 }}>
        Everything read in the meantime is kept, so when extraction arrives it runs over the mail
        already collected rather than starting from that day.
      </p>
    </div>
  );
}

function Stat({ n, label, sub }: { n: number; label: string; sub: string | null }) {
  return (
    <div className="card">
      <div
        className="gradient-text"
        style={{ fontSize: "2.5rem", fontWeight: 700, lineHeight: 1.1 }}
      >
        {n.toLocaleString()}
      </div>
      <div style={{ marginTop: 6, fontWeight: 600 }}>{label}</div>
      {sub && (
        <p className="body muted" style={{ marginTop: 8, fontSize: "0.8125rem" }}>
          {sub}
        </p>
      )}
    </div>
  );
}

/**
 * `empty` is deliberately per-row rather than one shared fallback.
 *
 * "Not mentioned" is extraction vocabulary — it means the AI read the email and
 * the field was not in it (§15). Reusing it for a mailbox that has simply never
 * done anything says something quite different and slightly wrong.
 */
function Row({ k, v, empty = "Not mentioned" }: { k: string; v: string | null; empty?: string }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>{v ?? empty}</span>
    </div>
  );
}

function stateLabel(me: Me): string {
  if (me.mailbox.status === null) return "No mailbox connected";
  if (me.mailbox.status === "needs_reauth") return "Needs reconnecting";
  if (me.mailbox.status === "disconnected") return "Disconnected";
  // Active but not yet subscribed: the consent landed and the subscription is
  // still being created. A brief, real state — not worth calling an error.
  return me.mailbox.ingestion_active ? "Reading new mail" : "Starting up";
}

/** "1 email" / "2 emails" — a plural s on a count of one reads as a bug. */
function plural(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}

function range(me: Me): string | null {
  const { oldest_received: oldest, newest_received: newest } = me.mailbox;
  if (!oldest || !newest) return null;
  const from = day(oldest);
  const to = day(newest);
  return from === to ? from : `${from} to ${to}`;
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Absolute, not "3 minutes ago": this page does not re-render on a timer, so
 *  a relative time would quietly age into a lie while someone reads it. */
function when(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
