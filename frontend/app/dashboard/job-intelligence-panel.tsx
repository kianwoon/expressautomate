"use client";

import { type ReactNode } from "react";

import { type Intelligence, type IntelligenceView, type NoIntelligence } from "./job-intelligence";
import { SalaryBenchmark, type Offer } from "./salary-benchmark";

/**
 * The three Job Intelligence stage panels — presentational only.
 *
 * The "Run analysis" button and all the analysis state (loading, polling,
 * starting, run-error) live in the `useJobIntelligence` hook, called once from
 * `Detail`. These components render a single stage each from the analysis the
 * hook produced, so each tab (Work / Person / Search) shows exactly the slice
 * of the result it owns.
 *
 * Each stage also owns its own empty/loading/failed notice, so a tab never
 * appears blank: it tells the reader whether the analysis has not run, is
 * running, or failed.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/** The shared state a stage needs to decide empty vs loading vs failed vs done.
 *  Lifted out so each stage panel reads the same shape without restating it. */
export type StageState = {
  hasAnalysis: boolean;
  waiting: boolean;
  failed: boolean;
  failureReason: string | null;
  loading: boolean;
  readError: string | null;
};

/** The nothing-yet line each intelligence tab shows before a run. */
// allow-hardcode: user-facing copy, not configuration.
const NOTHING_YET =
  'No analysis yet. Use "Run analysis" at the top to understand the work, the ideal person, and how to find them.';

function StageNotice({ state }: { state: StageState }) {
  if (state.hasAnalysis) return null;
  if (state.waiting)
    return (
      <p className="body src-state" aria-live="polite">
        <span className="src-pulse" aria-hidden="true" />
        Analysing this job order. This can take a minute or two — the three stages
        run in sequence.
      </p>
    );
  if (state.failed && state.failureReason)
    return (
      <p className="body src-error" role="alert">
        {state.failureReason}
      </p>
    );
  if (state.readError)
    return (
      <p className="body src-note" role="alert">
        {state.readError}
      </p>
    );
  if (state.loading) return <p className="body src-note">Checking for an analysis.</p>;
  return <p className="body src-note">{NOTHING_YET}</p>;
}

export function WorkStage({
  intelligence,
  state,
  offer,
}: {
  intelligence: Intelligence | null;
  state: StageState;
  offer: Offer;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const u = intelligence.understanding;
  return (
    <>
      <Stage title="Understanding the work">
        <Field label="Role" value={u.role} />
        <Field label="Purpose" value={u.business_purpose} />
        <List label="Daily activities" items={u.daily_activities} />
        <Field label="Environment" value={u.work_environment} />
        <Field label="Conditions" value={u.working_conditions} />
        <List label="Must have" items={u.must_have_requirements} />
        <List label="Nice to have" items={u.preferred_requirements} />
        <List label="What success looks like" items={u.success_characteristics} />
        <List label="Potential challenges" items={u.potential_challenges} />
      </Stage>
      {intelligence.occupation && (
        <SalaryBenchmark occupation={intelligence.occupation} offer={offer} />
      )}
    </>
  );
}

export function PersonStage({
  intelligence,
  state,
}: {
  intelligence: Intelligence | null;
  state: StageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const p = intelligence.persona;
  return (
    <Stage title="The ideal person">
      <List label="Likely backgrounds" items={p.likely_backgrounds} />
      <List label="Transferable roles" items={p.transferable_roles} />
      <List label="Transferable industries" items={p.transferable_industries} />
      <Field label="Career stage" value={p.career_stage} />
      <List label="Behaviours" items={p.behaviours} />
      <Field label="Communication" value={p.communication_style} />
      <List label="Motivations" items={p.motivations} />
      <Field label="Salary expectation" value={p.salary_expectation} />
      <Field label="Availability" value={p.availability} />
    </Stage>
  );
}

export function SearchStage({
  intelligence,
  state,
  view,
}: {
  intelligence: Intelligence | null;
  state: StageState;
  view: IntelligenceView | NoIntelligence | null;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const s = intelligence.search_plan;
  const removed = view && "removed_codes" in view ? view.removed_codes : null;
  return (
    <Stage title="How to find them">
      <Field label="Platform" value={s.platform} />
      <List label="Queries" items={s.queries} mono />
      <List label="Exclude" items={s.negative_queries} mono />
      <Field label="Salary" value={s.salary} />
      <Field label="Location" value={s.location} />
      <Field label="Employment type" value={s.employment_type} />
      {removed && removed.length > 0 && (
        <p className="body jo-sub jo-intel-redacted">
          {removed.length} protected-attribute code
          {removed.length === 1 ? "" : "s"} from the source ({removed.join(", ")}) were withheld
          from the analysis.
        </p>
      )}
    </Stage>
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
