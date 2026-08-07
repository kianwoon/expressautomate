"use client";

import { type ReactNode } from "react";

import { type CandidateIntelligence } from "./candidate-intelligence";

/**
 * The three Candidate Intelligence v2 stage panels — presentational only.
 *
 * Mirrors `job-intelligence-panel.tsx` in shape. The "Run analysis" button and
 * all the analysis state (loading, polling, starting, run-error) live in the
 * `useCandidateIntelligence` hook, called once from the candidate panel. These
 * components render a single stage each from the analysis the hook produced.
 *
 * The three tabs map the v2 engine's 8 conceptual layers to recruiter-readable
 * labels: History (L1+L2), Market fit (L3–L6), Residual value (L7+L8).
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/** The shared state a stage needs to decide empty vs loading vs failed vs done.
 *  Lifted out so each stage panel reads the same shape without restating it. */
export type CandidateStageState = {
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
  'No analysis yet. Use "Run analysis" at the top to reassess this candidate\'s experience against today\'s market.';

function StageNotice({ state }: { state: CandidateStageState }) {
  if (state.hasAnalysis) return null;
  if (state.waiting)
    return (
      <p className="body cand-intel-note">
        Analysing this candidate. This takes a few seconds.
      </p>
    );
  if (state.failed && state.failureReason)
    return (
      <p className="body cand-intel-error" role="alert">
        {state.failureReason}
      </p>
    );
  if (state.readError)
    return (
      <p className="body cand-intel-note" role="alert">
        {state.readError}
      </p>
    );
  if (state.loading) return <p className="body cand-intel-note">Checking for an analysis.</p>;
  return <p className="body cand-intel-note">{NOTHING_YET}</p>;
}

export function HistoryStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const h = intelligence.history;
  return (
    <Stage title="History">
      <RolesTable roles={h.roles} />
      <List label="Industries" items={h.industries} />
      <List label="Functions" items={h.functions} />
      <List label="Systems" items={h.systems} />
      <List label="Trajectory" items={h.trajectory} />
    </Stage>
  );
}

export function MarketFitStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const { automation, benchmark, gaps } = intelligence;
  return (
    <>
      <Stage title="Automation exposure">
        <AutomationList assessments={automation.assessments} />
        <List label="Scarce capabilities" items={automation.scarce_capabilities} />
      </Stage>
      <Stage title="Today's market benchmark">
        <Field label="Work family" value={benchmark.work_family} />
        <List label="Current work" items={benchmark.current_work} />
        <List label="Current required capabilities" items={benchmark.current_required} />
        <List label="Declining" items={benchmark.declining} />
        <List label="Emerging" items={benchmark.emerging} />
        <List label="Scarce" items={benchmark.scarce} />
        <Field label="Automation summary" value={benchmark.automation_summary} />
      </Stage>
      <Stage title="Gap analysis">
        <GapList gaps={gaps.gaps} />
        <List label="Evidence gaps to verify" items={gaps.evidence_gaps} />
      </Stage>
    </>
  );
}

export function ResidualValueStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const r = intelligence.residual;
  return (
    <>
      <Stage title="Residual value">
        <Field label="Historical strength" value={r.historical_strength} />
        <Field label="Automation exposure" value={r.automation_exposure} />
        <Field label="Current relevance" value={r.current_relevance} />
        <List label="Scarce capabilities" items={r.scarce_capabilities} />
        <List label="Depreciated capabilities" items={r.depreciated_capabilities} />
        <List label="Emerging capabilities" items={r.emerging_capabilities} />
        <List label="Evidence gaps" items={r.evidence_gaps} />
        <Field label="Overall assessment" value={r.overall_assessment} />
      </Stage>
      <Stage title="Current profile">
        {/* The candid paragraph the v2 engine's final layer produces. This is
            the headline output — a recruiter reads it to understand who the
            candidate is in today's market, not a years-of-experience summary. */}
        <p className="body cand-intel-profile">{r.current_profile}</p>
      </Stage>
    </>
  );
}

function Stage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="cand-intel-stage">
      <h4 className="cand-intel-stage-title">{title}</h4>
      <dl className="cand-intel-fields">{children}</dl>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="cand-intel-field">
      <dt className="cand-intel-dt">{label}</dt>
      <dd className="body">{value}</dd>
    </div>
  );
}

function List({ label, items }: { label: string; items: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="cand-intel-field">
      <dt className="cand-intel-dt">{label}</dt>
      <dd className="body">
        <ul className="cand-intel-list">
          {items.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

/** The automation-level pill colour. Colour is redundant to the label word
 *  (very_high → amber, low/very_low → teal), never the only signal — per the
 *  `.jo-quality` accessibility idiom. */
function automationLevelClass(level: string): string {
  const l = level.toLowerCase();
  if (l === "very_high") return "cand-intel-pill-amber";
  if (l === "high") return "cand-intel-pill-amber";
  if (l === "medium") return "cand-intel-pill-blue";
  if (l === "low") return "cand-intel-pill-teal";
  if (l === "very_low") return "cand-intel-pill-teal";
  return "cand-intel-pill-grey";
}

/** The gap-status pill colour. Demonstrated → teal, weak/contradicted → amber,
 *  not_evidenced → neutral grey (it is NOT a deficit — "absence of evidence is
 *  not evidence of absence"). */
function gapStatusClass(status: string): string {
  const s = status.toLowerCase();
  if (s === "demonstrated") return "cand-intel-pill-teal";
  if (s === "partially_demonstrated") return "cand-intel-pill-blue";
  if (s === "claimed_weak") return "cand-intel-pill-amber";
  if (s === "contradicted") return "cand-intel-pill-amber";
  return "cand-intel-pill-grey";
}

/** Human-readable label for the automation_level enum value. */
function automationLevelLabel(level: string): string {
  const l = level.toLowerCase();
  if (l === "very_high") return "Very high";
  if (l === "very_low") return "Very low";
  return level.charAt(0).toUpperCase() + level.slice(1);
}

/** Human-readable label for the gap status enum value. */
function gapStatusLabel(status: string): string {
  const s = status.toLowerCase();
  const labels: Record<string, string> = {
    demonstrated: "Demonstrated",
    partially_demonstrated: "Partially demonstrated",
    claimed_weak: "Claimed, weakly evidenced",
    not_evidenced: "Not evidenced",
    contradicted: "Contradicted",
  };
  return labels[s] ?? status;
}

function AutomationList({
  assessments,
}: {
  assessments: { capability: string; automation_level: string; automation_reason: string; residual_human_value: string }[];
}) {
  if (!assessments || assessments.length === 0) return null;
  return (
    <div className="cand-intel-field">
      <dt className="cand-intel-dt">Capability assessments</dt>
      <dd className="body">
        <ul className="cand-intel-card-list">
          {assessments.map((a, i) => (
            <li key={i} className="cand-intel-card">
              <div className="cand-intel-card-head">
                <span className="cand-intel-card-name">{a.capability}</span>
                <span
                  className={`cand-intel-pill ${automationLevelClass(a.automation_level)}`}
                >
                  {automationLevelLabel(a.automation_level)}
                </span>
              </div>
              {a.automation_reason && (
                <p className="cand-intel-card-reason">{a.automation_reason}</p>
              )}
              {a.residual_human_value && (
                <p className="cand-intel-card-residual">
                  <span className="cand-intel-card-label">Residual human value: </span>
                  {a.residual_human_value}
                </p>
              )}
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

function GapList({
  gaps,
}: {
  gaps: { capability: string; status: string; note: string }[];
}) {
  if (!gaps || gaps.length === 0) return null;
  return (
    <div className="cand-intel-field">
      <dt className="cand-intel-dt">Capabilities vs today&apos;s standard</dt>
      <dd className="body">
        <table className="cand-intel-table">
          <colgroup>
            <col className="cand-intel-col-cap" />
            <col className="cand-intel-col-status" />
            <col className="cand-intel-col-note" />
          </colgroup>
          <thead>
            <tr>
              <th className="cand-intel-th">Capability</th>
              <th className="cand-intel-th">Status</th>
              <th className="cand-intel-th">Note</th>
            </tr>
          </thead>
          <tbody>
            {gaps.map((g, i) => (
              <tr key={i} className="cand-intel-tr">
                <td className="cand-intel-td cand-intel-td-title">{g.capability}</td>
                <td className="cand-intel-td">
                  <span
                    className={`cand-intel-pill ${gapStatusClass(g.status)}`}
                  >
                    {gapStatusLabel(g.status)}
                  </span>
                </td>
                <td className="cand-intel-td cand-intel-td-note">{g.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </dd>
    </div>
  );
}

function RolesTable({
  roles,
}: {
  roles: {
    period: string;
    title: string;
    domain: string;
    seniority: string;
    scope: string;
    work: { task: string; tool: string; judgment_level: string; accountability: string }[];
    evidence: string;
  }[];
}) {
  if (!roles || roles.length === 0) return null;
  return (
    <div className="cand-intel-field">
      <dt className="cand-intel-dt">Roles & decomposed work</dt>
      <dd className="body">
        {/* Each role is a card: a header (period / title / domain / seniority)
            over the decomposed work items. The work decomposition is the L2
            input the automation stage reasons about, so it is shown in full
            rather than hidden behind the title. */}
        <ul className="cand-intel-role-list">
          {roles.map((role, i) => (
            <li key={i} className="cand-intel-role">
              <div className="cand-intel-role-head">
                <span className="cand-intel-role-period">{role.period || "—"}</span>
                <span className="cand-intel-role-title">{role.title}</span>
                {role.domain && (
                  <span className="cand-intel-role-domain">{role.domain}</span>
                )}
                {role.seniority && (
                  <span className="cand-intel-role-seniority">{role.seniority}</span>
                )}
              </div>
              {role.scope && <p className="cand-intel-role-scope">{role.scope}</p>}
              {role.work.length > 0 && (
                <ul className="cand-intel-work-list">
                  {role.work.map((w, j) => (
                    <li key={j} className="cand-intel-work-item">
                      <span className="cand-intel-work-task">{w.task}</span>
                      {w.tool && (
                        <span className="cand-intel-work-meta"> · {w.tool}</span>
                      )}
                      {w.judgment_level && (
                        <span className="cand-intel-work-judgment">{w.judgment_level}</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {role.evidence && (
                <p className="cand-intel-cap-evidence">&ldquo;{role.evidence}&rdquo;</p>
              )}
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}
