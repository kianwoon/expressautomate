"use client";

import { useState } from "react";

import {
  PLACEMENT_TYPES,
  updateOpportunityPlacement,
  type Opportunity,
  type PlacementType,
  type SexRequirement,
} from "./opportunities";

/**
 * What kind of placement this vacancy is, and — separately — whether the
 * job itself carries a genuine sex requirement.
 *
 * The two are unrelated facts on the same row: `placement_type` decides
 * which MOM rules the eligibility check runs (§ eligibility contract), and
 * the sex requirement is a job-specific occupational one — the case this was
 * built around is a family needing a female carer for an elderly parent's
 * intimate personal care. A requirement with no reason is exactly what
 * Singapore law does not permit, so the reason is required the moment a
 * requirement is set — enforced here so the field cannot even be submitted
 * empty, and enforced again by the backend's own check constraint, because
 * a form is not the only way a row gets written.
 *
 * allow-hardcode: the strings below are user-facing copy, not a list
 * anything is matched against.
 */

export function PlacementForm({
  row,
  onSaved,
}: {
  row: Opportunity;
  onSaved: (row: Opportunity) => void;
}) {
  const [placementType, setPlacementType] = useState<PlacementType | "">(row.placement_type ?? "");
  const [sexRequirement, setSexRequirement] = useState<SexRequirement | "">(
    row.sex_requirement ?? "",
  );
  const [reason, setReason] = useState(row.sex_requirement_reason ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The one rule this form enforces client-side: a requirement needs its
  // reason before the request is even sent. The backend enforces the same
  // pairing with a check constraint — this is the friendlier half of two
  // guards, not the only one.
  const reasonMissing = sexRequirement !== "" && reason.trim().length === 0;

  async function save() {
    if (saving || reasonMissing) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateOpportunityPlacement(row.id, {
        placement_type: placementType === "" ? null : placementType,
        sex_requirement: sexRequirement === "" ? null : sexRequirement,
        sex_requirement_reason: sexRequirement === "" ? null : reason.trim(),
      });
      onSaved(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not save this just now.");
    } finally {
      setSaving(false);
    }
  }

  const dirty =
    placementType !== (row.placement_type ?? "") ||
    sexRequirement !== (row.sex_requirement ?? "") ||
    reason !== (row.sex_requirement_reason ?? "");

  return (
    <div className="card jo-placement" id="placement-type">
      <span className="eyebrow">Placement type</span>
      <p className="body jo-sub">
        What kind of placement this is decides which Work Permit rules the eligibility check below
        runs. Leave it unset until you know — an unset placement type shows as &ldquo;needs a
        placement type&rdquo; on every candidate, never as a pass.
      </p>

      <label className="jo-placement-field">
        <span className="row-k">Placement type</span>
        <select
          value={placementType}
          onChange={(event) => setPlacementType(event.target.value as PlacementType | "")}
        >
          <option value="">Not set</option>
          {PLACEMENT_TYPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <label className="jo-placement-field">
        <span className="row-k">Sex requirement</span>
        <select
          value={sexRequirement}
          onChange={(event) => setSexRequirement(event.target.value as SexRequirement | "")}
        >
          <option value="">None</option>
          <option value="female">Female</option>
          <option value="male">Male</option>
        </select>
      </label>

      {sexRequirement !== "" && (
        <label className="jo-placement-field">
          <span className="row-k">Reason</span>
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why this job genuinely requires this sex — e.g. intimate personal care for an elderly client."
            rows={2}
          />
          <p className="body jo-sub jo-placement-hint">
            Required whenever a sex requirement is set. Singapore law only permits one where it is a
            genuine feature of the job itself — never a client preference — so the reason has to be
            on record before the requirement can be saved. This is separate from a coded preference
            like &ldquo;C/F&rdquo; found in a client email, which is decoded and flagged elsewhere
            but never acted on.
          </p>
        </label>
      )}

      <div className="jo-placement-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => void save()}
          disabled={saving || !dirty || reasonMissing}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {reasonMissing && (
          <p className="body jo-placement-hint" role="alert">
            Add a reason before saving a sex requirement.
          </p>
        )}
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
