"use client";

import { useEffect, useRef, useState } from "react";

import {
  PLACEMENT_TYPES,
  getOpportunity,
  setOpportunityOccupationalRequirement,
  setOpportunityPlacementType,
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

/** The three fields this form owns, as the empty-string form the selects use.
 *  Shared with the panel above so both decide "did this row's placement change"
 *  by the same rule. */
export function placementFields(row: Opportunity): {
  placementType: PlacementType | "";
  sexRequirement: SexRequirement | "";
  reason: string;
} {
  return {
    placementType: row.placement_type ?? "",
    sexRequirement: row.sex_requirement ?? "",
    reason: row.sex_requirement_reason ?? "",
  };
}

export function same(
  a: ReturnType<typeof placementFields>,
  b: ReturnType<typeof placementFields>,
): boolean {
  return (
    a.placementType === b.placementType &&
    a.sexRequirement === b.sexRequirement &&
    a.reason === b.reason
  );
}

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

  /**
   * A colleague's change to this row, once the panel above has passed it down.
   *
   * Two rules, and both matter. The trigger is the row's values changing, not
   * the row object changing — the object is new on every poll, and resyncing
   * on it would discard a half-made choice several times a minute. And an edit
   * in progress is never overwritten: silently swapping a recruiter's own
   * selection for someone else's is the same lost decision this exists to
   * prevent, only pointed the other way. Their choice stays on screen and Save
   * still sends it; the server holds whichever write lands last either way.
   *
   * The read-back inside `save` sets these fields too, and does so without
   * this effect's help — deliberately, because after a partial save the row
   * has not moved but the refused half has.
   */
  const seen = useRef(placementFields(row));
  useEffect(() => {
    const next = placementFields(row);
    const previous = seen.current;
    if (same(next, previous)) return;
    seen.current = next;
    const untouched = same(
      { placementType, sexRequirement, reason },
      previous,
    );
    if (!untouched) return;
    setPlacementType(next.placementType);
    setSexRequirement(next.sexRequirement);
    setReason(next.reason);
  }, [row, placementType, sexRequirement, reason]);

  // The one rule this form enforces client-side: a requirement needs its
  // reason before the request is even sent. The backend enforces the same
  // pairing with a check constraint — this is the friendlier half of two
  // guards, not the only one.
  const reasonMissing = sexRequirement !== "" && reason.trim().length === 0;

  /**
   * Two writes, not one, and only for the half that changed.
   *
   * The server keeps these apart deliberately: each route stamps its own
   * `set_by`/`set_at`, so writing both on every save would record a lawful
   * sex-requirement judgement against a recruiter who only picked a permit
   * type. Sending only what moved also halves the chance of a partial save.
   *
   * The placement type goes first and the requirement second, so a refusal of
   * the regulated half — the one the backend can still reject over its reason
   * — is the last thing to happen and is what the recruiter is told about.
   * Neither route can be rolled back from here, so instead of pretending the
   * save was atomic the form re-reads the row afterwards, whether or not
   * anything failed, and hands the panel the state the server actually holds.
   */
  async function save() {
    if (saving || reasonMissing) return;
    setSaving(true);
    setError(null);
    const nextPlacement = placementType === "" ? null : placementType;
    const nextRequirement = sexRequirement === "" ? null : sexRequirement;
    let failure: string | null = null;
    let wrote = false;
    try {
      if (nextPlacement !== (row.placement_type ?? null)) {
        await setOpportunityPlacementType(row.id, nextPlacement);
        wrote = true;
      }
      if (
        nextRequirement !== (row.sex_requirement ?? null) ||
        (nextRequirement !== null && reason.trim() !== (row.sex_requirement_reason ?? ""))
      ) {
        await setOpportunityOccupationalRequirement(row.id, {
          sex_requirement: nextRequirement,
          sex_requirement_reason: nextRequirement === null ? null : reason.trim(),
        });
        wrote = true;
      }
    } catch (err) {
      failure = err instanceof Error ? err.message : "We could not save this just now.";
    }

    try {
      // Read back even after a failure: the first write may well have landed,
      // and a panel left showing the values that were typed would be showing
      // something nobody stored. Also resync this form's own fields to the
      // read-back row: on a partial save (placement type wrote, requirement
      // was refused) the selects would otherwise keep showing the refused
      // value even though the panel above is now correct. This only happens
      // right after a write, not on every `row` prop change, so it cannot
      // clobber the recruiter's typing mid-edit from an unrelated poll.
      if (wrote || failure === null) {
        const fresh = await getOpportunity(row.id);
        onSaved(fresh);
        setPlacementType(fresh.placement_type ?? "");
        setSexRequirement(fresh.sex_requirement ?? "");
        setReason(fresh.sex_requirement_reason ?? "");
      }
    } catch (err) {
      failure ??= err instanceof Error ? err.message : "We could not save this just now.";
    }

    setError(failure);
    setSaving(false);
  }

  const dirty =
    placementType !== (row.placement_type ?? "") ||
    sexRequirement !== (row.sex_requirement ?? "") ||
    // Only while a requirement is set. With "None" chosen the reason field is
    // not even on screen, and there is nothing to save for it — Save skips the
    // pair, so counting it as a change offered a button that did nothing and
    // then wiped the typing on the read-back with no explanation.
    (sexRequirement !== "" && reason !== (row.sex_requirement_reason ?? ""));

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
