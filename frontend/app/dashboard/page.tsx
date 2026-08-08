"use client";

import { useEffect } from "react";

import { CONNECT_MAILBOX_PATH, LANDING_PATH, SWITCH_ACCOUNT_PATH } from "../api";
import { displayNameOf, useAuth, type Me } from "../auth";
import { SiteFooter } from "../site-footer";
import { SiteNav } from "../site-nav";
import { AccountDetails } from "./account-details";
import { ChoosePeriod } from "./choose-period";
import { JobOrders } from "./job-orders";

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
  // The one page that follows the event stream. Everything on it is live — mail
  // being read, the watch being set up, a grant going bad — and a dashboard that
  // asked once was a dashboard that told you what was true when you opened it
  // and then quietly aged. "Refresh in a moment" used to be an instruction on
  // this page; the server now says when there is something to see.
  const auth = useAuth(true);

  // Guard. Only a real 401 sends you away, and it goes to the landing page —
  // the user's own choice of provider belongs to them, so we never bounce
  // straight into the sign-in redirect. "unreachable" stays put and says so.
  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  // A running mailbox leads with the stat cards rather than a heading, so the
  // section above them needs less room. Decided here because the padding
  // belongs to the section, not to what fills it.
  const running = auth.status === "signed-in" && stageOf(auth.me) === "ingesting";

  return (
    <>
      <SiteNav />
      <main>
        {/* The hero's 64px top padding was sized for a landing page opening
            with a headline. With the heading gone for a running mailbox there
            is nothing in that space, so the page opened with a band of empty
            gradient between the nav and the first card. The other stages still
            lead with a heading and keep the room for it. */}
        <section className="hero" style={{ paddingTop: running ? 24 : undefined, paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            {auth.status === "signed-in" ? (
              <SignedIn me={auth.me} />
            ) : auth.status === "unreachable" ? (
              <Notice
                eyebrow="Connection"
                heading="We could not reach the server."
                body="This is not a sign-in problem — your session is untouched. The page keeps trying and will recover on its own. If this stays here, the service is down and we are looking at it."
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
      {/* The dashboard was the only page without one. Contact, terms and
          privacy are exactly what someone wants to hand a mailbox to a product
          — so the page where they do it is the worst one to omit them from. */}
      <SiteFooter />
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
type Stage = "none" | "reconnect" | "choose" | "starting" | "ingesting";

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
  // Permission granted, no mailbox yet: the user still owes us an answer to
  // "how far back?". This is the step that stops consent from silently
  // importing three months of someone's mail (§6.2).
  if (me.mailbox.awaiting_period) return "choose";
  return "none";
}

function SignedIn({ me }: { me: Me }) {
  const stage = stageOf(me);

  // A running mailbox needs no introduction: the nav already says whose
  // account this is, and the first stat card already says how much mail was
  // read, so "Signed in as X" and "We have read N emails" were the same two
  // facts a second time, occupying the top of the page. The other stages keep
  // a heading because on those the page IS about the mailbox — there is no
  // workspace under it to be the subject.
  const running = stage === "ingesting";

  return (
    <>
      {/* No eyebrow above the heading: "YOUR ACCOUNT" over "Signed in as …"
          labelled the page twice, and the nav already says whose account this
          is. The heading starts the page, like the other dashboard pages'. */}
      {!running && (
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
          Signed in as <span className="gradient-text">{displayNameOf(me)}</span>
        </h1>
      )}

      {stage === "ingesting" ? (
        <Ingesting me={me} />
      ) : stage === "reconnect" ? (
        <Reconnect />
      ) : stage === "choose" ? (
        // Reload rather than patching state locally: `/auth/me` is the only
        // thing that knows what the backend actually did, and guessing here is
        // how a dashboard starts disagreeing with the database.
        <ChoosePeriod onStarted={() => window.location.reload()} />
      ) : stage === "starting" ? (
        <Starting />
      ) : (
        <NotConnected me={me} />
      )}

      {/* The workspace comes before the account details, because it is what
          someone opened this page for. The cards that used to sit here answer
          questions asked once during setup — who am I signed in as, which
          workspace — and then repeated themselves above the fold on every
          visit forever. They are still available below, folded away.

          Shown whenever a mailbox is actually running, not only once a
          vacancy has been found: the stat cards, the sync activity and the
          mailbox overview are exactly what someone needs during the wait, and
          hiding them until the first extraction lands left the first hour
          looking like nothing was happening. */}
      {(stage === "ingesting" || me.mailbox.ingested.opportunities > 0) && (
        // Its heading becomes the page's h1 exactly when nothing above it is
        // one, so the outline never starts at h2 and never carries two h1s.
        <JobOrders me={me} heading={running ? "h1" : "h2"} />
      )}

      <AccountDetails me={me} />
    </>
  );
}

/**
 * One sentence. The numbers that used to sit under it are now the four stat
 * cards at the top of the workspace below, which are fed from the same
 * response as the table — so the headline figure and the list can no longer
 * disagree, as they did at 3 against 7 when one counted emails and the other
 * counted vacancies.
 */
function Ingesting({ me }: { me: Me }) {
  const { total } = me.mailbox.ingested;

  // Only the empty case says anything. Once mail is arriving, the count is on
  // the card immediately below and repeating it in a sentence was one fact
  // taking two rows at the top of the page. An empty mailbox has no card worth
  // reading, so it keeps its sentence.
  if (total > 0) return null;

  // Full width, like the job-orders table below: every sentence on this page
  // that sits above the workspace spans the same measure as the rows under it,
  // rather than stopping at `.lede`'s 62ch or a 62ch body cap.
  return (
    <p className="lede" style={{ marginTop: 18, maxWidth: "none" }}>
      {/* No delivery-time promise: Microsoft decides when a notification
          arrives, and a stated "minute or two" would be our claim to keep. */}
      Your mailbox is connected and being watched. Nothing has arrived yet — new mail appears here
      shortly after it reaches Outlook.
    </p>
  );
}

function Starting() {
  return (
    <>
      <p className="lede" style={{ marginTop: 18, maxWidth: "none" }}>
        Your mailbox is connected. We are setting up the watch on it now — this takes a few
        seconds, and mail starts arriving here once it is done.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "none" }}>
        {/* It used to say "Refresh in a moment", which was an instruction to do
            the page's job for it. The page now re-checks on its own, so the
            sentence describes that instead of delegating it. */}
        Nothing to do — this page moves on by itself once the watch is live.
      </p>
    </>
  );
}

function Reconnect() {
  return (
    <>
      <p className="lede" style={{ marginTop: 18, maxWidth: "none" }}>
        {/* "Picks up where it left off" only holds while the delta checkpoint
            is still valid; after a long enough outage Graph expires it and the
            walk restarts. Promising continuity outright would be a claim we
            cannot keep for the case that matters most. */}
        Microsoft has stopped letting us read this mailbox, so ingestion has paused. Nothing
        already read has been lost, and reconnecting resumes it.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "none" }}>
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
      <p className="lede" style={{ marginTop: 18, maxWidth: "none" }}>
        Your account is set up. Connect a mailbox and we will start reading the recruitment mail
        that arrives in it.
      </p>
      <p className="body" style={{ marginTop: 12, maxWidth: "none" }}>
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
