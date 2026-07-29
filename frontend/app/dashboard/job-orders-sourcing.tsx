"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { SOURCING_POLL_MS } from "../api";
import { flagged } from "./codes";
import { eligibilityFor, type Eligibility, type EligibilityFinding } from "./eligibility";
import type { Opportunity } from "./opportunities";
import {
  getSourcing,
  inFlight,
  namesFor,
  recordSubmission,
  startSourcing,
  type MatchReason,
  type SourcingMatch,
  type SourcingRun,
  type SourcingView,
} from "./sourcing";

/**
 * Who fits this job order, why, and who has actually been sent.
 *
 * The shortlist is a stored record, not a live query — a run is asked for
 * once and read back afterwards — so this section has three jobs: start one,
 * say honestly what it is doing while it works, and then show the reasoning
 * behind an order somebody else computed.
 *
 * Two of the things it must say are about what the shortlist did *not* do,
 * and both are load-bearing:
 *
 * - Where the job order asks for a protected characteristic, or the model
 *   noticed one in the text, the notice says plainly that the requirement was
 *   ignored. A safeguard nobody can see is indistinguishable from one that
 *   never ran, and the recruiter is the person who has to answer the client.
 * - Where the client behind the job order could not be resolved, the
 *   "already submitted to this client" exclusion did not run. The list may
 *   therefore contain someone already in front of that client, which is the
 *   re-pitching embarrassment the exclusion exists to prevent — so it is said
 *   out loud rather than left to be discovered.
 *
 * Nothing here parses `score` or a reason's numbers. They arrive as strings
 * from a NUMERIC column and the server has already ordered the list properly
 * (score descending, then candidate id); re-sorting strings numerically would
 * quietly reorder a shortlist a recruiter may already have sent.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

/** What each scoring component is called in front of a recruiter. An unknown
 *  name falls back to itself, so a component added server-side shows up as
 *  something rather than vanishing from the breakdown. */
const COMPONENT_LABELS: Record<string, string> = {
  title: "Job title",
  skills: "Skills",
  employer: "Employer",
  salary: "Salary",
  tenure: "Experience",
  recency: "Recent activity",
};

/** What each eligibility criterion is called in front of a recruiter. */
const CRITERION_LABELS: Record<string, string> = {
  sex: "Sex",
  age: "Age",
  education: "Education",
  nationality: "Source country",
  occupational_sex_requirement: "This job's sex requirement",
};

type View =
  | { status: "loading" }
  | { status: "ready"; data: SourcingView }
  // Kept apart from "no shortlist yet", and never collapsed into it: a failed
  // read rendered as "nobody has been shortlisted" is a claim about the
  // agency's candidates rather than about our server.
  | { status: "unreadable"; message: string };

export function Shortlist({ row }: { row: Opportunity }) {
  const [view, setView] = useState<View>({ status: "loading" });
  const [names, setNames] = useState<ReadonlyMap<string, string>>(new Map());
  // `null` per id means "could not be read", kept apart from "not fetched
  // yet" (the id simply absent) so a stale flag from a previous poll is never
  // shown while a fresh one is still in flight.
  const [eligibility, setEligibility] = useState<ReadonlyMap<string, Eligibility | null>>(
    new Map(),
  );
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<ReadonlySet<string>>(new Set());
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null);
  const [reclaimFocus, setReclaimFocus] = useState(false);
  const startRef = useRef<HTMLButtonElement | null>(null);

  // The names are joined here rather than expected from the sourcing routes,
  // which carry candidate ids and nothing else. Held in a ref so the fetch
  // below can skip ids it already has without naming `names` as a dependency
  // and re-running itself on its own result. One request per candidate is
  // only reasonable because a run keeps a bounded shortlist — the size of
  // this join is the cap the worker applied, not the size of the database.
  const known = useRef<ReadonlyMap<string, string>>(names);
  known.current = names;
  const knownEligibility = useRef<ReadonlyMap<string, Eligibility | null>>(eligibility);
  knownEligibility.current = eligibility;

  const refetch = useCallback(async () => {
    try {
      const data = await getSourcing(row.id);
      setView({ status: "ready", data });
      if (data.matches.length > 0) {
        const ids = data.matches.map((match) => match.candidate_id);
        setNames(await namesFor(ids, known.current));
        setEligibility(await eligibilityFor(row.id, ids, knownEligibility.current));
      }
    } catch (err) {
      setView((prev) =>
        // A failed poll keeps the shortlist it has. The rows on screen were
        // true when they were fetched; discarding them to report a blip loses
        // real information to describe a transient one.
        prev.status === "ready"
          ? prev
          : {
              status: "unreadable",
              message:
                err instanceof Error ? err.message : "We could not read this shortlist just now.",
            },
      );
    }
  }, [row.id]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const run = view.status === "ready" ? view.data.run : null;
  const waiting = inFlight(run);

  // Only while the run is `pending` or `running`. A `done` or `failed` run is
  // a finished record, and asking again is a request whose answer cannot
  // change — every open panel, forever, for nothing.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void refetch(), SOURCING_POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, refetch]);

  // Focus, deferred until the control can actually take it.
  //
  // Marking someone submitted removes the button that was pressed — there is
  // nothing left to press on that row — and left alone focus falls to the
  // body, so a keyboard user restarts at the top of the document. "Find
  // candidates" is the one control in this section that is always here.
  // Focusing it inside the handler is what has bitten this codebase before:
  // it is still `disabled` while a request is in flight, and a disabled
  // element silently refuses focus. So the intent is recorded and spent here,
  // once nothing is in flight and the button is real again.
  useEffect(() => {
    if (!reclaimFocus || starting || submitting) return;
    startRef.current?.focus();
    setReclaimFocus(false);
  }, [reclaimFocus, starting, submitting]);

  async function start() {
    if (starting) return;
    setStarting(true);
    setError(null);
    setRowError(null);
    try {
      const started = await startSourcing(row.id);
      // Shown immediately rather than waited for: the POST answers 202 with
      // the run itself, so the panel can say "queued" before the first poll.
      setView({ status: "ready", data: { run: started, matches: [] } });
      setSubmitted(new Set());
      setEligibility(new Map());
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not start a shortlist just now.");
    } finally {
      setStarting(false);
    }
  }

  async function submit(candidateId: string, clientId: string) {
    if (submitting) return;
    setSubmitting(candidateId);
    setRowError(null);
    try {
      await recordSubmission(candidateId, clientId, row.id);
      setSubmitted((prev) => new Set(prev).add(candidateId));
      setReclaimFocus(true);
    } catch (err) {
      setRowError({
        id: candidateId,
        message:
          err instanceof Error ? err.message : "We could not record that submission just now.",
      });
    } finally {
      setSubmitting(null);
    }
  }

  const matches = view.status === "ready" ? view.data.matches : [];

  return (
    <section className="src" aria-label="Shortlist">
      <div className="src-head">
        <span className="row-k">Shortlist</span>
        <button
          type="button"
          ref={startRef}
          className="src-start"
          onClick={() => void start()}
          disabled={starting || waiting}
        >
          {starting ? "Starting…" : run ? "Find candidates again" : "Find candidates"}
        </button>
      </div>

      {error && (
        <p className="body src-error" role="alert">
          {error}
        </p>
      )}

      {view.status === "loading" ? (
        <p className="body src-note">Checking for a shortlist.</p>
      ) : view.status === "unreadable" ? (
        <p className="body src-note" role="alert">
          {view.message}
        </p>
      ) : run === null ? (
        <p className="body src-note">
          Nobody has been shortlisted for this job order yet. Finding candidates scores everyone in
          your database against what this email actually said.
        </p>
      ) : (
        <>
          <RunState run={run} />
          <Safeguards row={row} run={run} />
          {run.state === "done" && matches.length === 0 && (
            <p className="body src-note">
              Nothing scored high enough to be worth showing. Adding skills or recent roles to your
              candidates is what changes this.
            </p>
          )}
          {matches.length > 0 && (
            <ol className="src-list">
              {matches.map((match, index) => (
                <Match
                  key={match.candidate_id}
                  match={match}
                  rank={index + 1}
                  name={names.get(match.candidate_id) ?? null}
                  eligibility={eligibility.get(match.candidate_id)}
                  clientId={run.client_id}
                  submitted={submitted.has(match.candidate_id)}
                  busy={submitting === match.candidate_id}
                  disabled={submitting !== null}
                  error={rowError?.id === match.candidate_id ? rowError.message : null}
                  onSubmit={(clientId) => void submit(match.candidate_id, clientId)}
                />
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}

/** What the run is doing, in words. Every state says something: a run that
 *  shows nothing is indistinguishable from a screen that has broken. */
function RunState({ run }: { run: SourcingRun }) {
  if (run.state === "pending") {
    return (
      <p className="body src-state" aria-live="polite">
        <span className="src-pulse" aria-hidden="true" />
        Queued. Nothing has been scored yet — this picks up within a few seconds.
      </p>
    );
  }
  if (run.state === "running") {
    return (
      <p className="body src-state" aria-live="polite">
        <span className="src-pulse" aria-hidden="true" />
        Reading your candidates and scoring them against this job order.
      </p>
    );
  }
  if (run.state === "failed") {
    return (
      <p className="body src-error" role="alert">
        {/* A worker-failed run has no sentence on it — only the route writes
            one, when queueing itself fails. An empty error box is worse than
            no box, so the fallback is a real sentence rather than whatever
            `failure_reason` happened to be. */}
        {run.failure_reason ??
          "This shortlist stopped before it finished, and nothing was recorded about why. Starting another one is safe."}
      </p>
    );
  }
  const considered = run.candidates_considered;
  return (
    <p className="body src-state" aria-live="polite">
      {considered === null
        ? "Done."
        : `Scored ${considered.toLocaleString()} candidate${considered === 1 ? "" : "s"}, best first.`}
    </p>
  );
}

/**
 * What the shortlist did not do.
 *
 * Both notices are about the limits of the list rather than its contents, so
 * they sit above it: a recruiter who reads them after picking someone has
 * read them too late.
 */
function Safeguards({ row, run }: { row: Opportunity; run: SourcingRun }) {
  // Either source counts. The glossary decoding catches shorthand in the
  // email; the model catches a requirement written out in prose that no code
  // covers. Neither is a superset of the other.
  const protectedNoticed = run.protected_attribute_noticed || flagged(row);

  return (
    <>
      {protectedNoticed && (
        <div className="src-notice" data-kind="protected">
          <p className="body">
            This job order asks for a protected characteristic. The shortlist <strong>ignored</strong>{" "}
            that requirement — nobody was ranked up or down for it. A coded requirement is removed
            from the text before the model sees it; a plainly-worded one the model does see, and
            reports back, so it can be ignored deliberately.
            {run.protected_attribute_note ? " The note below says which happened here." : ""}
          </p>
          {run.protected_attribute_note && (
            <p className="body src-sub">{run.protected_attribute_note}</p>
          )}
        </div>
      )}

      {run.client_unresolved_reason && (
        <div className="src-notice" data-kind="client">
          <p className="body">
            We could not tell which client this job order is for, so the{" "}
            <strong>already-submitted check did not run</strong>. Someone on this list may already
            be in front of that client.
          </p>
          <p className="body src-sub">{run.client_unresolved_reason}</p>
        </div>
      )}
    </>
  );
}

function Match({
  match,
  rank,
  name,
  eligibility,
  clientId,
  submitted,
  busy,
  disabled,
  error,
  onSubmit,
}: {
  match: SourcingMatch;
  rank: number;
  /** Null when the candidate record could not be read. The id is shown
   *  instead, which is at least traceable — never a blank row. */
  name: string | null;
  /** `undefined` while the fetch is still out, `null` if it failed, and the
   *  record itself once read. Three states, three different renders — see
   *  `EligibilityFlags`. */
  eligibility: Eligibility | null | undefined;
  /** The client the run excluded against, or null when it could not be told —
   *  in which case there is nothing to submit *to*. */
  clientId: string | null;
  submitted: boolean;
  busy: boolean;
  disabled: boolean;
  error: string | null;
  onSubmit: (clientId: string) => void;
}) {
  const who = name ?? match.candidate_id;

  return (
    <li className="src-row">
      <div className="src-row-main">
        <span className="src-rank" aria-hidden="true">
          {rank}
        </span>
        <span className={name ? "src-name" : "src-name src-name-unknown"}>{who}</span>
        {/* The score verbatim, exactly as the server computed and stored it.
            Rounding it here would collapse distinct scores into apparent ties
            in the one place a recruiter goes to ask why the order is what it
            is. */}
        <span className="src-score">{match.score}</span>
      </div>

      <EligibilityFlags eligibility={eligibility} />

      <Breakdown reasons={match.reasons} />

      {/* A match with no explanation is normal — only the top few are sent to
          a model, and a response that could not be verified against the CV is
          dropped rather than shown. It keeps its score either way, so the
          absence needs no apology and gets none. */}
      {match.explanation && <p className="body src-why">{match.explanation}</p>}
      {match.explanation_evidence && (
        <p className="body src-quote">
          <q>{match.explanation_evidence}</q>
        </p>
      )}

      <div className="src-row-acts">
        {submitted ? (
          <span className="src-done">Submitted</span>
        ) : clientId ? (
          <button
            type="button"
            className="src-act"
            disabled={disabled}
            onClick={() => onSubmit(clientId)}
          >
            {busy ? "Recording…" : `Mark ${who} submitted`}
          </button>
        ) : (
          // Disabled rather than silently failing: the POST needs a client
          // id, an unresolved run has none, and the runs where that is true
          // are exactly the runs where the exclusion could not be applied
          // either. Failing quietly there is the worst of both.
          <span className="src-act-off">
            Recording a submission needs a client, and this job order has none we could resolve.
          </span>
        )}
      </div>

      {error && (
        <p className="body src-error" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

/**
 * Placement eligibility for one candidate against this job order — a
 * compliance fact, never a verdict on the person and never a reason they
 * were ranked where they are.
 *
 * Four outcomes, four different renders, and `unknown` is the one that has
 * to look actionable rather than like a quiet failure:
 * - `met` and `not_applicable` are folded behind a one-line summary. Passing
 *   is normal; a rule that does not apply to this placement type is not
 *   even a fact about the person. Neither belongs at the volume of a
 *   warning.
 * - `not_met` is shown plainly, with the detail the record gives.
 * - `unknown` is shown as a task — a missing field, with a link to go fill
 *   it in — never as a failed check.
 *
 * No summary badge collapses this to a single pass/fail. A tick would have
 * to be four-valued to be honest, and the two values it would destroy —
 * `unknown` and `not_applicable` — are the two this exists to keep apart.
 */
function EligibilityFlags({ eligibility }: { eligibility: Eligibility | null | undefined }) {
  if (eligibility === undefined) {
    return <p className="body src-elig-note muted">Checking placement eligibility…</p>;
  }
  if (eligibility === null) {
    return (
      <p className="body src-elig-note" role="alert">
        We could not check placement eligibility for this candidate just now.
      </p>
    );
  }
  if (!eligibility.assessable) {
    return (
      <p className="body src-elig-note" data-kind="not-applicable">
        This job order has no placement type set, so eligibility has not been checked — that is a
        gap in the job order, not a finding about this candidate.{" "}
        <a href="#placement-type">Set the placement type above.</a>
      </p>
    );
  }

  const flagged = eligibility.findings.filter(
    (finding) => finding.outcome === "not_met" || finding.outcome === "unknown",
  );
  const quiet = eligibility.findings.filter(
    (finding) => finding.outcome === "met" || finding.outcome === "not_applicable",
  );

  return (
    <div className="src-elig">
      {flagged.map((finding) => (
        <EligibilityRow key={finding.criterion} finding={finding} />
      ))}
      {quiet.length > 0 && (
        <details className="src-elig-quiet">
          <summary className="body src-elig-summary">
            {quiet.length} criteri{quiet.length === 1 ? "on" : "a"} met or not applicable
          </summary>
          {quiet.map((finding) => (
            <EligibilityRow key={finding.criterion} finding={finding} />
          ))}
        </details>
      )}
      {eligibility.evaluated_as_of && (
        <p className="body src-elig-asof muted">
          Assessed as of {eligibility.evaluated_as_of} — MOM judges age at the time a permit is
          filed, not today.
        </p>
      )}
    </div>
  );
}

function EligibilityRow({ finding }: { finding: EligibilityFinding }) {
  const label = CRITERION_LABELS[finding.criterion] ?? finding.criterion;
  const basisLabel = finding.basis === "regulatory" ? "Work Permit rule" : "This job's requirement";

  return (
    <p className="body src-elig-row" data-outcome={finding.outcome}>
      <span className="src-elig-k">{label}</span>
      <span className="src-elig-basis muted">{basisLabel}</span>
      {finding.basis === "occupational" ? (
        <span className="src-elig-v">
          <q>{finding.detail}</q>
        </span>
      ) : (
        <span className="src-elig-v">{finding.detail}</span>
      )}
      {finding.outcome === "unknown" && (
        <a className="src-elig-fix" href="/dashboard/candidates">
          Fill this in on the candidate record.
        </a>
      )}
    </p>
  );
}

/** Why this person is where they are. The arithmetic behind the score, so
 *  "why is she third?" has an answer on the page rather than in a log. */
function Breakdown({ reasons }: { reasons: MatchReason[] | null }) {
  if (!reasons || reasons.length === 0) return null;

  return (
    <ul className="src-reasons">
      {reasons.map((reason) => (
        <li key={reason.name} className="src-reason">
          <span className="src-reason-k">{COMPONENT_LABELS[reason.name] ?? reason.name}</span>
          {/* Null means nothing was recorded to compare, which is not the same
              as scoring zero — saying "0" here would put "a poor fit on
              salary" in front of a recruiter when the truth is that nobody
              stated one. */}
          <span className={reason.contribution === null ? "src-reason-v muted" : "src-reason-v"}>
            {reason.contribution === null
              ? (reason.note ?? "Nothing to compare")
              : `${reason.contribution} of ${reason.weight}`}
          </span>
          {reason.contribution !== null && reason.note && (
            <span className="src-reason-note">{reason.note}</span>
          )}
        </li>
      ))}
    </ul>
  );
}
