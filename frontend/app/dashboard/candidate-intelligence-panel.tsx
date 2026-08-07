"use client";

import { type ReactNode } from "react";

import { type CandidateIntelligence } from "./candidate-intelligence";

/**
 * The three Candidate Intelligence stage panels — presentational only.
 *
 * Mirrors `job-intelligence-panel.tsx` in shape. The "Run analysis" button and
 * all the analysis state (loading, polling, starting, run-error) live in the
 * `useCandidateIntelligence` hook, called once from the candidate panel. These
 * components render a single stage each from the analysis the hook produced.
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
  'No analysis yet. Use "Run analysis" at the top to understand this candidate\'s career, capabilities, and the roles they could fit.';

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

export function CareerStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const c = intelligence.career;
  return (
    <Stage title="Career">
      <Timeline entries={c.timeline} />
      <List label="Trajectory" items={c.trajectory} />
      <Field label="Primary domain" value={c.primary_domain} />
      <List label="Secondary domains" items={c.secondary_domains} />
      <Field label="Direction" value={c.career_direction} />
      <Field label="Stage" value={c.career_stage} />
    </Stage>
  );
}

export function CapabilityStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const cap = intelligence.capability;
  return (
    <Stage title="Capabilities">
      {cap.capabilities.length > 0 ? (
        <div className="cand-intel-field">
          <dt className="cand-intel-dt">Demonstrated capabilities</dt>
          <dd className="body">
            <ul className="cand-intel-cap-list">
              {cap.capabilities.map((entry, i) => (
                <li key={i} className="cand-intel-cap">
                  <span className="cand-intel-cap-head">
                    <span className="cand-intel-cap-name">{entry.capability}</span>
                    <span className="cand-intel-cap-cat">{entry.category}</span>
                    <span className="cand-intel-cap-conf">
                      {Math.round(entry.confidence * 100)}%
                    </span>
                  </span>
                  {entry.supporting_evidence && (
                    <span className="cand-intel-cap-evidence">
                      &ldquo;{entry.supporting_evidence}&rdquo;
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </dd>
        </div>
      ) : null}
      <List label="Tools" items={cap.tools} />
    </Stage>
  );
}

export function ProfileStage({
  intelligence,
  state,
}: {
  intelligence: CandidateIntelligence | null;
  state: CandidateStageState;
}) {
  if (!intelligence) return <StageNotice state={state} />;
  const p = intelligence.profile;
  return (
    <Stage title="Professional profile">
      <Field label="Professional identity" value={p.professional_identity} />
      <List label="Specializations" items={p.specializations} />
      <Field label="Orientation" value={p.orientation} />
      <RoleAffinityList affinities={p.role_affinity} />
    </Stage>
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

function Timeline({
  entries,
}: {
  entries: { period: string; title: string; domain: string }[];
}) {
  if (!entries || entries.length === 0) return null;
  return (
    <div className="cand-intel-field">
      <dt className="cand-intel-dt">Timeline</dt>
      <dd className="body">
        <ul className="cand-intel-timeline">
          {entries.map((entry, i) => (
            <li key={i} className="cand-intel-tl-row">
              <span className="cand-intel-tl-period">{entry.period || "—"}</span>
              <span className="cand-intel-tl-title">{entry.title}</span>
              {entry.domain && (
                <span className="cand-intel-tl-domain">{entry.domain}</span>
              )}
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

function RoleAffinityList({
  affinities,
}: {
  affinities: { role: string; affinity_type: string; confidence: number }[];
}) {
  if (!affinities || affinities.length === 0) return null;
  // Group by affinity type so a recruiter reads direct fits, then adjacent,
  // then transferable — the order the design doc (Phase 7) presents them in.
  const groups: Record<string, { role: string; confidence: number }[]> = {};
  for (const a of affinities) {
    const key = a.affinity_type || "other";
    if (!groups[key]) groups[key] = [];
    groups[key].push({ role: a.role, confidence: a.confidence });
  }
  const order = ["direct_fit", "adjacent", "transferable", "other"];
  const labels: Record<string, string> = {
    direct_fit: "Direct-fit roles",
    adjacent: "Adjacent roles",
    transferable: "Transferable roles",
    other: "Other",
  };
  return (
    <>
      {order
        .filter((key) => groups[key] && groups[key].length > 0)
        .map((key) => (
          <List
            key={key}
            label={labels[key]}
            items={groups[key].map(
              (g) => `${g.role} (${Math.round(g.confidence * 100)}%)`,
            )}
          />
        ))}
    </>
  );
}
