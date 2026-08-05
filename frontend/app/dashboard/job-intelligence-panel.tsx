"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";

import {
  type Intelligence,
  type IntelligenceView,
  getIntelligence,
  runIntelligence,
} from "./job-intelligence";
import { type Opportunity } from "./opportunities";

/**
 * The "Job Intelligence" section of the job order modal.
 *
 * Reads the stored analysis on mount (so an analysis a colleague ran is there
 * when the modal opens) and offers a button to run or re-run it. Full-width,
 * like the Shortlist it sits beside, because the three stages each read best
 * across the whole modal.
 *
 * Three states are kept distinct, the same way `Shortlist` keeps them:
 *  - `loading` — the first read is in flight, and we do not yet know whether an
 *    analysis exists;
 *  - `idle` with a null analysis — the read succeeded and there is nothing yet;
 *  - `error` — the read failed, which is never the same as "nothing yet".
 */

type Phase =
  | { status: "loading" }
  | { status: "idle"; view: IntelligenceView }
  | { status: "error"; message: string };

export function JobIntelligence({ row }: { row: Opportunity }) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [running, setRunning] = useState(false);
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

  async function run() {
    if (running) return;
    setRunning(true);
    setRunError(null);
    try {
      const view = await runIntelligence(row.id);
      setPhase({ status: "idle", view });
    } catch (err) {
      setRunError(err instanceof Error ? err.message : "The analysis could not run just now.");
    } finally {
      setRunning(false);
    }
  }

  const analysis = phase.status === "idle" ? phase.view.intelligence : null;

  return (
    <section className="src jo-intel" aria-label="Job Intelligence">
      <div className="src-head">
        <span className="row-k">Job Intelligence</span>
        <button
          type="button"
          className="src-start"
          onClick={() => void run()}
          disabled={running}
        >
          {running ? "Analysing…" : analysis ? "Re-run analysis" : "Run analysis"}
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

      {phase.status === "idle" && analysis === null && (
        <p className="body src-note">
          No analysis yet. “Run analysis” reads this job order and explains the work, the kind of
          person who would do it well, and how to find them.
        </p>
      )}

      {phase.status === "idle" && phase.view.intelligence !== null && (
        <Analysis
          intelligence={phase.view.intelligence}
          removedCodes={phase.view.removed_codes}
          analysedAt={phase.view.analysed_at}
        />
      )}
    </section>
  );
}

function Analysis({
  intelligence,
  removedCodes,
  analysedAt,
}: {
  intelligence: Intelligence;
  removedCodes: string[] | null;
  analysedAt: string | null;
}) {
  return (
    <div className="jo-intel-body">
      {analysedAt && (
        <p className="body jo-sub jo-intel-when">
          Last analysed {new Date(analysedAt).toLocaleString()}.
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

      {removedCodes && removedCodes.length > 0 && (
        <p className="body jo-sub jo-intel-redacted">
          {removedCodes.length} protected-attribute code
          {removedCodes.length === 1 ? "" : "s"} from the source ({removedCodes.join(", ")}) were
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
