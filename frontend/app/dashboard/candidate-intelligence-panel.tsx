"use client";

import { type ReactNode } from "react";

import { type CandidateIntelligence } from "./candidate-intelligence";

/**
 * The Candidate Intelligence v2 panels — Assessment, Work, Education.
 *
 * Assessment is the sharp read (headline + summary + scarce/depreciated/unproven).
 * Work is the decomposed work units (the evidence behind the assessment).
 * Education is the qualifications table.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export type CandidateStageState = {
  hasAnalysis: boolean;
  waiting: boolean;
  failed: boolean;
  failureReason: string | null;
  loading: boolean;
  readError: string | null;
};

// allow-hardcode: user-facing copy, not configuration.
const NOTHING_YET =
  'No analysis yet. Use "Run analysis" at the top to assess this candidate against today\'s market.';

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

// ---------------------------------------------------------------------------
// ASSESSMENT — the sharp read (the default tab)
// ---------------------------------------------------------------------------

export function AssessmentStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const a = intelligence.assessment;
  return (
    <>
      {/* The headline — the one-line read a recruiter opens with. Large,
          bold, the first thing they see. */}
      <p className="cand-intel-headline">{a.headline}</p>
      <p className="cand-intel-summary">{a.summary}</p>

      <div className="cand-intel-stats">
        <Stat label="Work level" value={a.work_level} />
        <Stat label="Decision authority" value={a.decision_authority} />
        <Stat label="AI exposure" value={a.ai_exposure} />
        <Stat label="Hire readiness" value={a.hire_readiness} />
        <Stat label="Value trajectory" value={a.value_trajectory} />
      </div>

      {a.scarce_capabilities.length > 0 && (
        <Section title="What remains scarce">
          {a.scarce_capabilities.map((s, i) => (
            <div key={i} className="cand-intel-cap">
              <span className="cand-intel-cap-name">{s.capability}</span>
              {s.evidence && <p className="cand-intel-cap-reason">{s.evidence}</p>}
            </div>
          ))}
        </Section>
      )}

      {a.depreciated_capabilities.length > 0 && (
        <Section title="What has depreciated">
          {a.depreciated_capabilities.map((d, i) => (
            <div key={i} className="cand-intel-cap">
              <span className="cand-intel-cap-name">{d.capability}</span>
              {d.reason && <p className="cand-intel-cap-reason">{d.reason}</p>}
            </div>
          ))}
        </Section>
      )}

      {a.unproven_claims.length > 0 && (
        <Section title="Unproven claims — ask in interview">
          {a.unproven_claims.map((u, i) => (
            <div key={i} className="cand-intel-unproven">
              <p className="cand-intel-unproven-claim">&ldquo;{u.claim}&rdquo;</p>
              {u.question && (
                <p className="cand-intel-unproven-question">→ {u.question}</p>
              )}
            </div>
          ))}
        </Section>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// WORK — the decomposed work units (the evidence)
// ---------------------------------------------------------------------------

export function WorkStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const roles = intelligence.work.roles;
  return (
    <>
      {roles.map((role, i) => (
        <div key={i} className="cand-intel-stage">
          <div className="cand-intel-role-head">
            <span className="cand-intel-role-period">{role.period}</span>
            <span className="cand-intel-role-title">{role.stated_title}</span>
            {role.employer && (
              <span className="cand-intel-role-domain">{role.employer}</span>
            )}
          </div>
          {role.contribution_maturity && (
            <p className="cand-intel-role-scope">
              Contribution: {role.contribution_maturity}
              {role.tenure_months ? ` · ${role.tenure_months}mo` : ""}
            </p>
          )}
          {role.work_units.map((wu, j) => (
            <div key={j} className="cand-intel-workunit">
              <div className="cand-intel-workunit-head">
                <span className="cand-intel-workunit-work">{wu.work}</span>
                {wu.inflated && <span className="cand-intel-pill cand-intel-pill-amber">⚠ Inflated</span>}
              </div>
              {wu.claim && wu.claim !== wu.work && (
                <p className="cand-intel-workunit-claim">CV says: &ldquo;{wu.claim}&rdquo;</p>
              )}
              {wu.evidence_note && (
                <p className="cand-intel-workunit-note">{wu.evidence_note}</p>
              )}
              <div className="cand-intel-workunit-tags">
                {wu.decision_ownership && (
                  <span className="cand-intel-pill cand-intel-pill-grey">
                    Decision: {wu.decision_ownership}
                  </span>
                )}
                {wu.complexity && (
                  <span className={`cand-intel-pill ${complexityClass(wu.complexity)}`}>
                    {wu.complexity}
                  </span>
                )}
                {wu.ai_heavy_lift && (
                  <span className={`cand-intel-pill ${aiClass(wu.ai_heavy_lift)}`}>
                    AI: {aiLabel(wu.ai_heavy_lift)}
                  </span>
                )}
                {wu.evidence && (
                  <span className={`cand-intel-pill ${evidenceClass(wu.evidence)}`}>
                    Evidence: {wu.evidence}
                  </span>
                )}
              </div>
              {wu.human_residual && (
                <p className="cand-intel-workunit-residual">
                  Human residual: {wu.human_residual}
                </p>
              )}
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// EDUCATION
// ---------------------------------------------------------------------------

export function EducationStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const edu = intelligence.work.education;
  if (!edu || edu.length === 0)
    return <p className="body cand-intel-note">No education data.</p>;
  return (
    <Section title="Education & qualifications">
      <table className="cand-intel-table">
        <colgroup>
          <col className="cand-intel-col-edu-period" />
          <col className="cand-intel-col-edu-qual" />
          <col className="cand-intel-col-edu-inst" />
        </colgroup>
        <thead>
          <tr>
            <th className="cand-intel-th">Period</th>
            <th className="cand-intel-th">Qualification</th>
            <th className="cand-intel-th">Institution</th>
          </tr>
        </thead>
        <tbody>
          {edu.map((e, i) => (
            <tr key={i} className="cand-intel-tr">
              <td className="cand-intel-td cand-intel-td-period">{e.period || "—"}</td>
              <td className="cand-intel-td cand-intel-td-title">
                {e.qualification}
                {e.field && <span className="cand-intel-td-sub">{e.field}</span>}
              </td>
              <td className="cand-intel-td cand-intel-td-note">{e.institution || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Stat({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="cand-intel-stat">
      <dt className="cand-intel-stat-label">{label}</dt>
      <dd className="cand-intel-stat-value">{value}</dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="cand-intel-stage">
      <h4 className="cand-intel-stage-title">{title}</h4>
      <div className="cand-intel-section-body">{children}</div>
    </div>
  );
}

function complexityClass(c: string): string {
  const s = c.toLowerCase();
  if (s === "expert" || s === "specialist") return "cand-intel-pill-teal";
  if (s === "skilled") return "cand-intel-pill-blue";
  return "cand-intel-pill-grey";
}

function aiClass(a: string): string {
  const s = a.toLowerCase();
  if (s.includes("dominant") || s.includes("agentic")) return "cand-intel-pill-amber";
  if (s.includes("heavy")) return "cand-intel-pill-amber";
  if (s.includes("assisted")) return "cand-intel-pill-blue";
  return "cand-intel-pill-teal";
}

function aiLabel(a: string): string {
  return a.replace(/ai_/g, "").replace(/_/g, " ");
}

function evidenceClass(e: string): string {
  const s = e.toUpperCase();
  if (s === "A" || s === "B") return "cand-intel-pill-teal";
  if (s === "C") return "cand-intel-pill-amber";
  return "cand-intel-pill-grey";
}
