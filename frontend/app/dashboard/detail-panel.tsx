"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { useAuth } from "../auth";
import { Breakable } from "../breakable";
import { type ClientMatch, ClientSearch } from "./client-search";
import { DecodedCodes, ProtectedBadge, flagged } from "./codes";
import { Dialog } from "./dialog";
import { type TabKey, TabBar } from "./detail-panel-tabs";
import { Salary, Value, day } from "./format";
import { PersonStage, SearchStage, WorkStage, type StageState } from "./job-intelligence-panel";
import { useJobIntelligence } from "./job-intelligence";
import { PlacementForm, placementFields, same } from "./job-order-placement";
import { Shortlist } from "./job-orders-sourcing";
import { MemberSelect } from "./member-picker";
import { type MutationResult, type Opportunity } from "./opportunities";
import { Initials } from "./person";
import { QualityNote, ReviewBadge } from "./quality";
import { ShareDialog } from "./share-dialog";

/**
 * One job order in full, as a popup over the list.
 *
 * The table can only ever show the short fields; requirements and description
 * are paragraphs, and clamping them to four lines in a cell was always a
 * compromise. Here they are simply shown. As a modal the list keeps the whole
 * width while it is closed and yields to it the moment a row is opened.
 *
 * The panel never invents a heading for a field the email did not mention. It
 * shows the field and says "Not mentioned" — an absence a recruiter can see is
 * useful, an absence that is silently omitted looks like a field we do not
 * extract.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

type Ownership = {
  onClaim: (id: string) => Promise<MutationResult>;
  onAssign: (id: string, userId: string | null) => Promise<MutationResult>;
  /** Files this job order under a client — and, when `adopt` is set and
   *  nobody holds it yet, asks the server to pass it to that client's own
   *  recruiter. Ownership, so it goes through the same read-back the claim and
   *  assign paths use rather than being a field save. */
  onClientSet: (
    id: string,
    clientId: string | null,
    adopt: boolean,
  ) => Promise<MutationResult>;
  /** The row went out from under the open modal — a share withdrawn, or an
   *  owner reassigning. The list is told so it can drop the selection; closing
   *  the modal is the parent's job, since it owns the `selected` state. */
  onVanished: (id: string) => void;
};

/**
 * The 403 message depends on the row, not only on the status.
 *
 * The server says the same thing either way — this job order is not yours to
 * move. But on a row nobody was ever assigned, "shared with you, not assigned
 * to you" is simply untrue, and it sends the reader looking for a colleague
 * who does not exist. The honest sentence there names the way out: claim it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */
function forbiddenMessage(row: Opportunity): string {
  return row.assigned_user_id
    ? "This job order is shared with you, not assigned to you."
    : "Claim this job order before editing it.";
}

export function DetailPanel({
  row,
  onClose,
  onReview,
  onClaim,
  onAssign,
  onClientSet,
  onVanished,
}: {
  row: Opportunity;
  /** Closes the modal. The parent owns the `selected` row; this just clears it. */
  onClose: () => void;
  onReview: (id: string, reviewed: boolean) => Promise<string | null>;
} & Ownership) {
  // Keyed on the row id so the pending state and any error reset when the
  // selection moves. Without the key, an error from marking one row reviewed
  // would still be sitting under the next row someone clicked.
  return (
    <Detail
      key={row.id}
      row={row}
      onClose={onClose}
      onReview={onReview}
      onClaim={onClaim}
      onAssign={onAssign}
      onClientSet={onClientSet}
      onVanished={onVanished}
    />
  );
}

function Detail({
  row,
  onClose,
  onReview,
  onClaim,
  onAssign,
  onClientSet,
  onVanished,
}: {
  row: Opportunity;
  onClose: () => void;
  onReview: (id: string, reviewed: boolean) => Promise<string | null>;
} & Ownership) {
  const auth = useAuth();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reviewed = row.review_status === "reviewed";

  // Ownership keeps its own pending flag and its own message. Sharing them
  // with the review toggle would grey out a button whose action is unrelated,
  // and would put a claim failure under the sentence about review state.
  const [moving, setMoving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // The panel's own copy of the placement fields, so a save is reflected
  // immediately without waiting for the next page fetch to bring `row` back
  // around. Reset by the `key={row.id}` this component is mounted under, the
  // same trick `saving`/`error` above rely on.
  const [placement, setPlacement] = useState(row);
  // The list polls, so a colleague's change to this same row's placement
  // fields arrives here as a fresh `row` — and used to stop there, leaving the
  // recruiter reading values nobody holds any more.
  //
  // Synced off the row's own placement VALUES, never off the object: every
  // poll hands this component a new object with identical fields, and
  // resyncing on identity would wipe whatever is being typed a few times a
  // minute. The last values seen are remembered rather than compared against
  // local state, because a save leaves this copy fresher than the list for a
  // poll or two — comparing against local state would flip it back to the
  // stale row and then forward again.
  const seenPlacement = useRef(placementFields(row));
  useEffect(() => {
    const next = placementFields(row);
    if (same(next, seenPlacement.current)) return;
    seenPlacement.current = next;
    setPlacement(row);
  }, [row]);
  // Sharing is offered to everyone who can see the row, not only to whoever
  // holds it: seeing a job order is the right to pass it to a named colleague,
  // and that chain is how a vacancy reaches the desk that can fill it. What a
  // recipient may not do — broadcast it to the office — is refused inside the
  // dialog, with the reason showing.
  const [sharing, setSharing] = useState(false);

  // The client this job order is filed under, or the one the recruiter has
  // picked and not yet sent. Pre-filled from the row, which carries the name
  // beside the id: without it a linked job order and an unlinked one look
  // identical here, and the only signal of a link is the absence of a
  // sentence — which is no way to check eight rows that were just filed.
  // Reset per row by the `key={row.id}` above, like the fields around it.
  const [client, setClient] = useState<ClientMatch | null>(
    row.client_id && row.client_name ? { id: row.client_id, name: row.client_name } : null,
  );
  // Defaulted on: the whole reason to link a job order to a client is that the
  // client is somebody's account, and the common case is that the account
  // holder should be working this one. An unassigned job order is the only
  // place it can apply, so it is the only place it is offered.
  const [adopt, setAdopt] = useState(true);
  // Its own line, not the ownership block's. See `move`.
  const [clientNotice, setClientNotice] = useState<string | null>(null);

  // Job Intelligence: one hook owns the analysis so the header button (Run) and
  // the three stage tabs (Work / Person / Search) share one source of truth —
  // one `run()`, one polling loop, one result. The hook is keyed on `row.id`
  // implicitly (it takes the id, not the row), so it follows the same
  // remount-on-row-change lifecycle the `key={row.id}` above gives this whole
  // component.
  const ji = useJobIntelligence(row.id);
  // Which tab is showing. Lives here, not in the hook: the hook owns the
  // analysis, the modal owns the layout. Survives row polls (same `key` rule
  // that keeps `placement` alive); resets when a different row is opened.
  const [activeTab, setActiveTab] = useState<TabKey>("origin");

  // The Origin tab is the tallest (it carries the facts, actions, prose and
  // shortlist). Once measured, its height is locked as the panel's min-height
  // so switching to a shorter Work/Person/Search tab does not collapse the
  // modal — every tab renders against the same canvas, and the height stops
  // jumping. A ResizeObserver tracks Origin's height as it settles (the
  // shortlist arrives after mount and grows the panel), and only while Origin
  // is the active tab. The locked height persists across tab switches.
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [panelMinHeight, setPanelMinHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (activeTab !== "origin" || !panelRef.current) return;
    const el = panelRef.current;
    const measure = () => {
      const h = el.scrollHeight;
      if (h > 0) setPanelMinHeight(h);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [activeTab]);

  async function runAnalysis() {
    await ji.run();
    // Jump to the Work tab so the recruiter watches the result arrive, rather
    // than left looking at the Origin tab wondering where the analysis went.
    setActiveTab("work");
  }

  async function toggle() {
    if (saving) return;
    setSaving(true);
    setError(null);
    setError(await onReview(row.id, !reviewed));
    setSaving(false);
  }

  /** Runs one ownership move and decides what the reader is left looking at.
   *
   * Three outcomes, not two. A 404 is not an error to display beside the
   * fields — the fields are stale the moment it arrives — so the modal is told
   * to close and reports upward. A 403 is re-worded against the row. Everything
   * else is the sentence the API already chose; nothing here re-derives copy.
   *
   * The branch is on `result.kind`, never on the sentence. Comparing copy made
   * the wording load-bearing: re-phrasing or translating one string would
   * silently turn "close the modal" into "show a red line", with no type error
   * and no failing test.
   *
   * `say` is where the sentence lands, because the modal has two ownership
   * controls in two places on the page. A failure from the client field
   * belongs under the client field: routing it to the claim block would put a
   * message about a company under a button about a person, and on a row whose
   * claim block is not rendered at all it would go nowhere. */
  async function move(
    run: () => Promise<MutationResult>,
    say: (message: string | null) => void = setNotice,
  ) {
    if (moving) return;
    setMoving(true);
    say(null);
    const result = await run();
    // Released on every path, including the one that closes the modal. It is
    // only invisible there because the body unmounts, and a pending flag left
    // true is a trap for whatever renders next.
    setMoving(false);
    if (result.ok) return;
    if (result.kind === "gone") {
      // The parent clears `selected`, unmounting this modal in the same
      // commit — so there is no message to render here, only the close to
      // hand upward. (When this was an always-open panel, `gone` carried the
      // sentence on a blank panel; now the modal simply disappears.)
      onVanished(row.id);
      return;
    }
    say(result.kind === "forbidden" ? forbiddenMessage(row) : result.message);
  }

  // Who may hand this on: the agency owner, and whoever is holding it. A
  // colleague it was merely shared with sees the owner's name and no control —
  // an offer that always fails is worse than no offer.
  // `signed-in` carries a `Me`, and a `Me` has a user. No `?.` here: a
  // defensive one only ever hid a bad test fixture, and hiding it invited the
  // next reader of `useAuth()` in this tree to copy the same shrug.
  const signedIn = auth.status === "signed-in" ? auth.me.user : null;
  const canAssign =
    signedIn != null && (signedIn.role === "owner" || signedIn.id === row.assigned_user_id);

  // Close is suppressed while a save or an ownership move is in flight, so
  // Escape or a backdrop click cannot discard a review toggle or a claim that
  // the server has not answered yet. The handlers below still run; only the
  // modal refuses to leave until they settle.
  const busy = saving || moving;

  // The shared state every intelligence stage reads to decide empty vs loading
  // vs failed. Derived once from the hook so the three stage panels render
  // consistently.
  const stageState: StageState = {
    hasAnalysis: !!ji.analysis,
    waiting: ji.waiting,
    failed: ji.view !== null && "state" in ji.view && ji.view.state === "failed",
    failureReason:
      ji.view !== null && "failure_reason" in ji.view ? ji.view.failure_reason : null,
    loading: ji.phase.status === "loading",
    readError: ji.phase.status === "error" ? ji.phase.message : null,
  };

  return (
    <Dialog
      title={
        <span className="jo-detail-title-row">
          <span className="jo-detail-title-text">
            <Value text={row.job_title_raw ?? "Job order"} />
          </span>
          {/* "Run analysis" beside the title so it is always reachable, whatever
              tab is showing. The state it drives lives in the `ji` hook; this
              button only calls it and reflects its flags. */}
          <button
            type="button"
            className="jo-detail-run"
            onClick={() => void runAnalysis()}
            disabled={ji.starting || ji.waiting}
          >
            {ji.starting
              ? "Starting…"
              : ji.waiting
                ? "Analysing…"
                : ji.analysis
                  ? "Re-run analysis"
                  : "Run analysis"}
          </button>
        </span>
      }
      titleId="jo-detail-title"
      onClose={busy ? () => {} : onClose}
      className="dlg-modal-wide jo-detail-modal"
    >
      {ji.runError && (
        <p className="body jo-detail-error" role="alert">
          {ji.runError}
        </p>
      )}

      {/* Header strip: the badges and the two lines that identify the row,
          across the full width so the title the Dialog rendered above reads
          as the heading of the whole record, not of one column. */}
      <div className="jo-detail-head">
        <span className="eyebrow">Details</span>
        <ReviewBadge status={row.review_status} />
        {/* Beside the review state rather than buried below the fields: it is
            a reason to read this row, so it has to be visible before anyone
            decides whether to. */}
        {flagged(row) && <ProtectedBadge />}
      </div>

      <p className="jo-detail-company">
        <Value text={row.company_name_raw} />
      </p>

      {/* Above the fields, not below them: whose job order this is changes
          whether the rest is yours to act on, so it has to be read first.
          The buddy who referred the client IS the owner — they brought the
          account in and manage the relationship. "Unassigned" is when no
          buddy is linked yet. */}
      <p className="jo-detail-owner" data-testid="jo-detail-owner">
        <span className="row-k">Owner</span>
        {row.buddy_name ? (
          <span>{row.buddy_name}</span>
        ) : row.assigned_user_id && row.assignee_name ? (
          <>
            <Initials name={row.assignee_name} seed={row.assigned_user_id} size={22} />
            <span>{row.assignee_name}</span>
          </>
        ) : (
          <span className="muted">Unassigned</span>
        )}
        {row.shared_with_me && <span className="jo-detail-shared">Shared with you</span>}
      </p>

      {/* The four sections of the record. Origin is the operational core (facts,
          actions, prose, shortlist); Work / Person / Search are the three Job
          Intelligence stages, each fed by the one `ji` hook. The bar is always
          visible so a recruiter can step between them without scrolling. */}
      <TabBar active={activeTab} onSelect={setActiveTab} />

      {/* One container for every tab's content. `minHeight` is the Origin
          height (measured once it renders), locked so the modal stops
          collapsing and re-growing on every tab switch. */}
      <div ref={panelRef} className="jo-tab-panel" style={panelMinHeight ? { minHeight: panelMinHeight } : undefined}>

      {/*
        Two columns, not one. The modal has 880px of width, and the content
        falls into two kinds: what the email said (the facts, read-only) and
        what we do about it (placement, ownership, share, review). Stacked
        singly they were a tall ribbon the recruiter scrolled through; paired,
        reading and acting sit side by side and the whole record is one glance.

        Left: the email's facts — client, quality, the extracted fields,
        decoded shorthand, provenance. Right: your actions — placement form,
        find candidates, claim/assign, share, review. The long prose and the
        shortlist answer the email rather than describing it, so they span
        both columns at full width below.

        The grid collapses to one column under ~640px (see the CSS), where
        two narrow columns would wrap every control inside itself.
      */}
      {activeTab === "origin" && (
      <div className="jo-modal-grid">
        {/* ---- Left: the email's facts ---- */}
        <div className="jo-modal-col">
          {/* Directly under the company name the extraction read off the
              email, because the two are the same question asked twice: that
              line is what the sender wrote, and this is which client on our
              books it turned out to be. Nothing joins them automatically —
              five of the eight unlinked rows in production name six candidate
              companies between them, which is exactly why a person chooses. */}
          <div className="jo-detail-client">
            <ClientSearch
              value={client}
              onChange={setClient}
              label="Client"
              hint="Search for the company on our books. Leave it empty if they are not on it yet."
            />

            {/* Only while nobody holds it. An assigned job order never changes
                hands on a link, so offering the choice here would be offering
                something that does nothing. */}
            {row.assigned_user_id === null && (
              <label className="jo-detail-adopt">
                <input
                  type="checkbox"
                  checked={adopt}
                  onChange={(event) => setAdopt(event.target.checked)}
                />
                <span>Also take on this client&rsquo;s recruiter</span>
              </label>
            )}

            <button
              type="button"
              className="btn btn-secondary"
              onClick={() =>
                void move(() => onClientSet(row.id, client?.id ?? null, adopt), setClientNotice)
              }
              disabled={moving || (client === null && row.client_id === null)}
            >
              {moving ? "Saving…" : "Link this client"}
            </button>

            {/* Said out loud, because the company name sits directly above and
                a blank field under it reads as "linked to that one". */}
            {row.client_id === null && (
              <p className="body jo-sub jo-detail-hint">
                This job order is not linked to a client, so it is not on anyone&rsquo;s account yet.
              </p>
            )}

            {clientNotice && (
              <p className="body jo-detail-error" role="alert">
                {clientNotice}
              </p>
            )}
          </div>

          <QualityNote row={row} />

          <div className="rows jo-detail-rows">
            <Row k="Received" v={day(row.received_datetime)} />
            <Row k="Location" v={row.location_raw} />
            <Row k="Hours" v={row.working_hours_raw} />
            <Row k="Duration" v={row.duration_raw} />
            <div className="row">
              <span className="row-k">Salary</span>
              <span>
                <Salary row={row} />
              </span>
            </div>
          </div>

          <DecodedCodes row={row} />

          {/* Provenance, not a link. We hold an id for the message, not a URL
              we can promise still resolves in the user's Outlook — offering
              one that 404s is worse than offering none. The id is here so a
              disputed row can be traced back to the email it came from. */}
          <div className="jo-detail-source">
            <span className="row-k">Source</span>
            <p className="body jo-sub">
              Read from one email in the connected mailbox. Every value above is the sender&rsquo;s,
              not ours.
            </p>
            <div className="rows">
              <Row k="Message id" v={row.internet_message_id} empty="Not recorded" />
              <Row k="Graph id" v={row.graph_message_id} empty="Not recorded" />
            </div>
          </div>
        </div>

        {/* ---- Right: your actions ---- */}
        <div className="jo-modal-col">
          <PlacementForm row={placement} onSaved={setPlacement} />

          {/* Carries this job order to the candidates page rather than growing
              a job-order picker over there — the recruiter already knows which
              role they are filling. A plain link, not a fetch: the candidates
              page reads `eligible_for` from the URL itself and does its own
              409 handling if the placement type turns out not to be set. */}
          <p className="jo-detail-find">
            <a
              className="btn btn-secondary"
              href={`/dashboard/candidates?eligible_for=${encodeURIComponent(placement.id)}`}
            >
              Find candidates for this role
            </a>
          </p>

          {/* Claim sits among the actions, deliberately not on a list row:
              taking one is taking responsibility for it, and that decision
              should follow reading the row rather than a scan of the table at
              9pm. */}
          {(row.assigned_user_id === null || canAssign) && (
            <div className="jo-detail-own">
              {row.assigned_user_id === null && (
                <>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void move(() => onClaim(row.id))}
                    disabled={moving}
                  >
                    {moving ? "Claiming…" : "Claim this job order"}
                  </button>
                  <p className="body jo-sub jo-detail-hint">
                    Nobody is working this one yet. Claiming it puts your name on it and makes it
                    yours to edit.
                  </p>
                </>
              )}

              {canAssign && (
                <MemberSelect
                  value={row.assigned_user_id}
                  onChange={(userId) => void move(() => onAssign(row.id, userId))}
                  allowNone
                  label="Assign to"
                />
              )}

              {notice && (
                <p className="body jo-detail-error" role="alert">
                  {notice}
                </p>
              )}
            </div>
          )}

          <div className="jo-detail-actions">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setSharing(true)}
            >
              Share this job order
            </button>
            <p className="body jo-sub jo-detail-hint">
              Hand it to a colleague who can fill it. They get to read it and pass it on — not to
              edit it.
            </p>
          </div>

          {sharing && <ShareDialog row={row} onClose={() => setSharing(false)} />}

          <div className="jo-detail-actions">
            <button
              type="button"
              className={reviewed ? "btn btn-secondary" : "btn btn-primary"}
              onClick={toggle}
              disabled={saving}
              aria-pressed={reviewed}
            >
              {saving ? "Saving…" : reviewed ? "Mark as not reviewed" : "Mark as reviewed"}
            </button>
            <p className="body jo-sub jo-detail-hint">
              {reviewed
                ? "Someone has checked this row against the email."
                : "Marking it reviewed records that a person has read this against the email. It changes nothing about the extraction."}
            </p>
          </div>
        </div>

        {/* ---- Full-width: the long reading + the answer to it ---- */}
        <Prose k="Requirements" text={row.requirements} />
        <Prose k="Description" text={row.job_description} />

        {/* Below the job order itself, above the provenance: the shortlist is
            an answer to what the email asked for, so it only makes sense once
            the requirements above have been read. Keyed by the row along with
            the rest of the panel, so moving the selection starts it over
            rather than leaving one job order's shortlist under another's
            title. Keyed on the placement fields too, on top of the row id
            `Detail` is already keyed by: saving a new placement type changes
            which MOM rules every candidate below is checked against, and the
            eligibility fetch inside `Shortlist` caches by candidate id with no
            way to know the job order under it just changed. Remounting is what
            forces it to ask again rather than show yesterday's answer next to
            today's placement type. */}
        <Shortlist
          key={`${placement.placement_type ?? ""}-${placement.sex_requirement ?? ""}`}
          row={row}
        />
      </div>
      )}

      {/* ---- Work / Person / Search: the three Job Intelligence stages, each
              one tab, each fed by the same `ji` hook. Each panel owns its own
              empty/loading/failed notice via `stageState`, so a tab never looks
              blank before the analysis has run. ---- */}
      {activeTab === "work" && (
        <WorkStage
          intelligence={ji.analysis}
          state={stageState}
          offer={{
            min: row.salary_min,
            max: row.salary_max,
            currency: row.salary_currency,
            period: row.salary_period,
            raw: row.salary_raw,
          }}
        />
      )}
      {activeTab === "person" && <PersonStage intelligence={ji.analysis} state={stageState} />}
      {activeTab === "search" && (
        <SearchStage intelligence={ji.analysis} state={stageState} view={ji.view} />
      )}
      </div>

      {/* A failure has to say so. Silently leaving the badge unchanged would
          let someone believe they had signed off a row they had not. */}
      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}

function Row({ k, v, empty }: { k: string; v: string | null; empty?: string }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>
        {v ? <Breakable text={v} /> : (empty ?? "Not mentioned")}
      </span>
    </div>
  );
}

function Prose({ k, text }: { k: string; text: string | null }) {
  return (
    <div className="jo-detail-prose">
      <span className="row-k">{k}</span>
      <p className={text ? "body" : "body muted"}>
        {text ? <Breakable text={text} /> : "Not mentioned"}
      </p>
    </div>
  );
}
