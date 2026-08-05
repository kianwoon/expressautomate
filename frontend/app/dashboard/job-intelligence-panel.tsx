"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";

import { SOURCING_POLL_MS } from "../api";
import {
  type Intelligence,
  type IntelligenceView,
  type NoIntelligence,
  getIntelligence,
  inFlight,
  runIntelligence,
} from "./job-intelligence";
import { type Opportunity } from "./opportunities";

/**
 * The "Job Intelligence" section of the job order modal.
 *
 * The analysis runs as a background job, so this panel does what the sourcing
 * panel does: read the row on mount, start it with a POST that returns 202 +
 * `pending`, and poll GET until the row is `done` or `failed`. The three
 * Cerebras calls run in the worker — not here, not in the request.
 *
 * States are kept distinct the way the sourcing panel keeps them:
 *  - `loading` — the first read is in flight;
 *  - `idle` — the read succeeded (row may be `pending`, `done`, `failed`, or
 *    absent entirely);
 *  - `error` — the read failed, which is never the same as "absent".
 */

type Phase =
  | { status: "loading" }
  | { status: "idle"; view: IntelligenceView | NoIntelligence }
  | { status: "error"; message: string };

export function JobIntelligence({ row }: { row: Opportunity }) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    try {
      const view = await getIntelligence(row.id);
      setPhase({ status: "idle", view });
    } catch (err) {
      setPhase({
        status: "error",
        message: err instanceof Error ? err.message : "We could not read this analysis just now.",
      });
    }
  }, [row.id]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const view = phase.status === "idle" ? phase.view : null;
  const waiting = inFlight(view);

  // Poll only while the row is `pending` or `running`. A `done` or `failed`
  // row is a finished record — asking again is a request whose answer cannot
  // change, every open panel, forever, for nothing.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void refetch(), SOURCING_POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, refetch]);

  async function run() {
    if (starting) return;
    setStarting(true);
    setRunError(null);
    try {
      const started = await runIntelligence(row.id);
      setPhase({ status: "idle", view: started });
    } catch (err) {
      setRunError(err instanceof Error ? err.message : "The analysis could not start just now.");
    } finally {
      setStarting(false);
    }
  }

  const state =
    view && "state" in view ? (view as IntelligenceView).state : null;
  const analysis = view && "intelligence" in view && view.intelligence ? view.intelligence : null;
  const failureReason =
    view && "failure_reason" in view ? view.failure_reason : null;

  return (
    <section className="src jo-intel" aria-label="Job Intelligence">
      <div className="src-head">
        <span className="row-k">Job Intelligence</span>
        <button
          type="button"
          className="src-start"
          onClick={() => void run()}
          disabled={starting || waiting}
        >
          {starting
            ? "Starting…"
            : waiting
              ? "Analysing…"
              : analysis
                ? "Re-run analysis"
                : "Run analysis"}
        </button>
      </div>

      {runError && (
        <p className="body src-error" role="alert">
          {runError}
        </p>
      )}

      {phase.status === "loading" && <p className="body src-note">Checking for an analysis.</p>}

      {phase.status === "error" && (
        <p className="body src-note" role="alert">
          {phase.message}
        </p>
      )}

      {phase.status === "idle" && view && !("state" in view) && (
        <p className="body src-note">
          No analysis yet. “Run analysis” reads this job order and explains the work, the kind of
          person who would do it well, and how to find them.
        </p>
      )}

      {waiting && (
        <p className="body src-note">
          Analysing this job order — understanding the work, inferring the ideal person, planning
          the search. This takes a few seconds.
        </p>
      )}

      {state === "failed" && failureReason && (
        <p className="body src-error" role="alert">
          {failureReason}
        </p>
      )}

      {analysis && view && "removed_codes" in view && (
        <Analysis intelligence={analysis} view={view as IntelligenceView} />
      )}
    </section>
  );
}

function Analysis({
  intelligence,
  view,
}: {
  intelligence: Intelligence;
  view: IntelligenceView;
}) {
  const { removed_codes, analysed_at } = view;
  return (
    <div className="jo-intel-body">
      {analysed_at && (
        <p className="body jo-sub jo-intel-when">
          Last analysed {new Date(analysed_at).toLocaleString()}.
        </p>
      )}

      <Stage title="Understanding the work">
        <Field label="Role" value={intelligence.understanding.role} />
        <Field label="Purpose" value={intelligence.understanding.business_purpose} />
        <List label="Daily activities" items={intelligence.understanding.daily_activities} />
        <Field label="Environment" value={intelligence.understanding.work_environment} />
        <Field label="Conditions" value={intelligence.understanding.working_conditions} />
        <List label="Must have" items={intelligence.understanding.must_have_requirements} />
        <List label="Nice to have" items={intelligence.understanding.preferred_requirements} />
        <List
          label="What success looks like"
          items={intelligence.understanding.success_characteristics}
        />
        <List
          label="Potential challenges"
          items={intelligence.understanding.potential_challenges}
        />
      </Stage>

      <Stage title="The ideal person">
        <List label="Likely backgrounds" items={intelligence.persona.likely_backgrounds} />
        <List label="Transferable roles" items={intelligence.persona.transferable_roles} />
        <List
          label="Transferable industries"
          items={intelligence.persona.transferable_industries}
        />
        <Field label="Career stage" value={intelligence.persona.career_stage} />
        <List label="Behaviours" items={intelligence.persona.behaviours} />
        <Field label="Communication" value={intelligence.persona.communication_style} />
        <List label="Motivations" items={intelligence.persona.motivations} />
        <Field label="Salary expectation" value={intelligence.persona.salary_expectation} />
        <Field label="Availability" value={intelligence.persona.availability} />
      </Stage>

      <Stage title="How to find them">
        <Field label="Platform" value={intelligence.search_plan.platform} />
        <List label="Queries" items={intelligence.search_plan.queries} mono />
        <List label="Exclude" items={intelligence.search_plan.negative_queries} mono />
        <Field label="Salary" value={intelligence.search_plan.salary} />
        <Field label="Location" value={intelligence.search_plan.location} />
        <Field label="Employment type" value={intelligence.search_plan.employment_type} />
      </Stage>

      {removed_codes && removed_codes.length > 0 && (
        <p className="body jo-sub jo-intel-redacted">
          {removed_codes.length} protected-attribute code
          {removed_codes.length === 1 ? "" : "s"} from the source ({removed_codes.join(", ")}) were
          withheld from the analysis.
        </p>
      )}
    </div>
  );
}

function Stage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="jo-intel-stage">
      <h4 className="jo-intel-stage-title">{title}</h4>
      <dl className="jo-intel-fields">{children}</dl>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="jo-intel-field">
      <dt className="jo-sub">{label}</dt>
      <dd className="body">{value}</dd>
    </div>
  );
}

function List({
  label,
  items,
  mono = false,
}: {
  label: string;
  items: string[];
  mono?: boolean;
}) {
  if (!items || items.length === 0) return null;
  return (
    <div className="jo-intel-field">
      <dt className="jo-sub">{label}</dt>
      <dd className="body">
        <ul className={mono ? "jo-intel-queries" : "jo-intel-list"}>
          {items.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      </dd>
    </div>
  );
}
