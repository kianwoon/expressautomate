"use client";

import { useState, type FormEvent, type ReactNode } from "react";

import {
  ApiError,
  DOMAIN_FIELD,
  FieldError,
  createClient,
  createContact,
  deleteContact,
  updateClient,
  updateContact,
  type Client,
  type ClientInput,
  type Contact,
  type ContactInput,
} from "../clients";
import { Dialog } from "../dialog";

/**
 * Create and edit a client, in one form.
 *
 * A sibling of `candidates/candidate-form.tsx` and built the same way: one
 * component for both modes, `null` for create; blank inputs become `null`
 * rather than `""` (§15 — an unset fee is blank, not 0); and an edit sends
 * only the fields that actually changed, because `PATCH /clients/{id}` uses
 * `exclude_unset`. Sending the whole form would turn every untouched empty
 * input into an explicit `null` and wipe fields nobody looked at.
 *
 * It lives outside `client-panel.tsx` on purpose — that file is already past
 * 450 lines, and the panel's job is to show a record, not to own a
 * twenty-control form.
 *
 * Two server refusals are shown in the server's own words, never replaced:
 *
 * - a free-provider email domain is a 422, and it is rendered against the
 *   domain field, because that is the field the recruiter has to change;
 * - a domain another client already holds is a 409 naming that client and
 *   saying what to do about it, rendered above the form where it cannot be
 *   missed.
 *
 * Contacts have their own endpoints and need a client id, so on create the
 * client is POSTed first and the returned id used for the contact calls. If
 * the client saves and a contact then fails, the dialog stays open holding
 * the new id — a second attempt edits that client rather than creating a
 * duplicate.
 *
 * allow-hardcode: the labels, placeholders and sentences below are
 * user-facing form copy rendered to the page, not values anything is matched
 * against.
 */

type FormState = {
  name: string;
  email_domain: string;
  website: string;
  phone: string;
  address: string;
  fee_percent: string;
  payment_terms_days: string;
  notes: string;
};

function toFormState(row: Client | null): FormState {
  return {
    name: row?.name ?? "",
    email_domain: row?.email_domain ?? "",
    website: row?.website ?? "",
    phone: row?.phone ?? "",
    address: row?.address ?? "",
    fee_percent: row?.fee_percent != null ? String(row.fee_percent) : "",
    payment_terms_days: row?.payment_terms_days != null ? String(row.payment_terms_days) : "",
    notes: row?.notes ?? "",
  };
}

/** Blank becomes `null`, never `""` and never 0. A fee nobody stated is not a
 *  fee of zero percent, and the backend's "Not mentioned" convention means an
 *  absent fact must actually be absent. */
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function numberOrNull(value: string): number | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : Number(trimmed);
}

function toClientInput(form: FormState): ClientInput {
  return {
    name: form.name.trim(),
    email_domain: orNull(form.email_domain),
    website: orNull(form.website),
    phone: orNull(form.phone),
    address: orNull(form.address),
    fee_percent: numberOrNull(form.fee_percent),
    payment_terms_days: numberOrNull(form.payment_terms_days),
    notes: orNull(form.notes),
  };
}

/** Only the keys whose value differs from the record this form was populated
 *  from. Nothing rides along unconditionally — `PATCH` treats an absent key as
 *  "leave it alone", so the smallest possible body is also the safest one. */
function changedClientFields(next: ClientInput, before: ClientInput): Partial<ClientInput> {
  const out: Partial<ClientInput> = {};
  (Object.keys(next) as (keyof ClientInput)[]).forEach((key) => {
    if (next[key] !== before[key]) {
      (out as Record<string, unknown>)[key] = next[key];
    }
  });
  return out;
}

/** A row in the contacts editor. `id` is `null` until the row has been saved
 *  once; `key` is stable across renders so React does not remount a row the
 *  recruiter is typing into when one above it is removed. */
type ContactRow = {
  key: string;
  id: string | null;
  name: string;
  email: string;
  phone: string;
  title: string;
  is_primary: boolean;
};

let contactKeySeq = 0;
function newContactKey(): string {
  contactKeySeq += 1;
  return `new-${contactKeySeq}`;
}

function toContactRows(contacts: Contact[] | undefined): ContactRow[] {
  return (contacts ?? []).map((c) => ({
    key: c.id,
    id: c.id,
    name: c.name,
    email: c.email ?? "",
    phone: c.phone ?? "",
    title: c.title ?? "",
    is_primary: c.is_primary,
  }));
}

function toContactInput(row: ContactRow): ContactInput {
  return {
    name: row.name.trim(),
    email: orNull(row.email),
    phone: orNull(row.phone),
    title: orNull(row.title),
  };
}

function isBlankRow(row: ContactRow): boolean {
  return (
    row.name.trim() === "" &&
    row.email.trim() === "" &&
    row.phone.trim() === "" &&
    row.title.trim() === ""
  );
}

/** Which of a contact's editable fields the recruiter actually changed. Same
 *  reason as the client body: `PATCH /clients/{id}/contacts/{id}` is
 *  `exclude_unset` too, so an untouched blank must not be sent as a `null`.
 *  `is_primary` is deliberately not part of this — it is settled in one pass
 *  at the end, because the server demotes the previous primary as a side
 *  effect and the order of the calls therefore matters. */
function changedContactFields(
  next: ContactInput,
  before: ContactInput,
): Partial<ContactInput> {
  const out: Partial<ContactInput> = {};
  (Object.keys(next) as (keyof ContactInput)[]).forEach((key) => {
    if (next[key] !== before[key]) {
      (out as Record<string, unknown>)[key] = next[key];
    }
  });
  return out;
}

export function ClientForm({
  client,
  onDone,
  onCancel,
}: {
  /** `null` for create. */
  client: Client | null;
  onDone: (saved: Client) => void;
  /** Called with the id of a client that was actually created before the
   *  recruiter cancelled — non-null only when create succeeded but a
   *  subsequent contact call failed, the one case where a real row exists on
   *  the server and the caller's list has not heard about it yet. `null`
   *  otherwise, so a plain cancel-with-nothing-created stays a no-op. */
  onCancel: (createdId: string | null) => void;
}) {
  const [form, setForm] = useState<FormState>(() => toFormState(client));
  // Captured once at mount so an edit is diffed against what the record held,
  // not against whatever the form holds by the time Save is pressed.
  const [initial] = useState<ClientInput>(() => toClientInput(toFormState(client)));
  const [initialContacts] = useState<ContactRow[]>(() => toContactRows(client?.contacts));
  const [rows, setRows] = useState<ContactRow[]>(() => toContactRows(client?.contacts));
  const [removedIds, setRemovedIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [domainError, setDomainError] = useState<string | null>(null);
  // Set when a create succeeded but a contact call afterwards did not. The
  // client exists from that moment on, so a retry must edit it.
  const [createdId, setCreatedId] = useState<string | null>(null);

  const editingId = client?.id ?? createdId;

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function setRow(key: string, patch: Partial<ContactRow>) {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((prev) => [
      ...prev,
      { key: newContactKey(), id: null, name: "", email: "", phone: "", title: "", is_primary: false },
    ]);
  }

  function removeRow(key: string) {
    setRows((prev) => {
      const going = prev.find((r) => r.key === key);
      if (going?.id) setRemovedIds((ids) => [...ids, going.id as string]);
      return prev.filter((r) => r.key !== key);
    });
  }

  /** A single "primary" across the group: choosing one clears the rest here,
   *  and the server does the same to the stored rows when it is told. */
  function choosePrimary(key: string) {
    setRows((prev) => prev.map((r) => ({ ...r, is_primary: r.key === key })));
  }

  const liveRows = rows.filter((r) => !isBlankRow(r));
  const namelessRow = liveRows.some((r) => r.name.trim() === "");
  const canSubmit = form.name.trim() !== "" && !namelessRow && !saving;

  /** Everything the contacts section owes the server, in an order that makes
   *  the last write the winning one: removals, then the field edits and the
   *  new rows, then the single primary. */
  async function saveContacts(clientId: string): Promise<void> {
    for (const id of removedIds) {
      await deleteContact(clientId, id);
    }
    setRemovedIds([]);

    // Ids as we know them after this pass — a row created just now has one
    // only from its response.
    const saved: { row: ContactRow; id: string }[] = [];
    for (const row of liveRows) {
      if (row.id === null) {
        const created = await createContact(clientId, toContactInput(row));
        setRow(row.key, { id: created.id });
        saved.push({ row, id: created.id });
        continue;
      }
      const before = initialContacts.find((r) => r.id === row.id);
      const changes = before
        ? changedContactFields(toContactInput(row), toContactInput(before))
        : toContactInput(row);
      if (Object.keys(changes).length > 0) {
        await updateContact(clientId, row.id, changes);
      }
      saved.push({ row, id: row.id });
    }

    // The primary, settled last and only when it moved. `createContact`
    // defaults `is_primary` to false, so a brand-new primary always needs
    // this call; an existing one that was already primary never does.
    const chosen = saved.find((s) => s.row.is_primary);
    if (chosen) {
      const wasPrimary = initialContacts.some((r) => r.id === chosen.id && r.is_primary);
      if (!wasPrimary) await updateContact(clientId, chosen.id, { is_primary: true });
      return;
    }
    // Nobody is primary any more. The server only demotes as a side effect of
    // promoting someone, so clearing has to be said out loud.
    const wasPrimary = initialContacts.find((r) => r.is_primary);
    if (wasPrimary?.id && !removedIds.includes(wasPrimary.id)) {
      const stillHere = liveRows.some((r) => r.id === wasPrimary.id);
      if (stillHere) await updateContact(clientId, wasPrimary.id, { is_primary: false });
    }
  }

  function showFailure(err: unknown, fallback: string) {
    if (err instanceof FieldError && err.field === DOMAIN_FIELD) {
      setDomainError(err.message);
      return;
    }
    setError(err instanceof ApiError ? err.message : fallback);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSaving(true);
    setError(null);
    setDomainError(null);

    const full = toClientInput(form);
    let savedClient: Client;

    try {
      if (editingId) {
        const changes = changedClientFields(full, initial);
        // Nothing changed on the client itself — the recruiter only touched a
        // contact. No PATCH at all rather than an empty one.
        savedClient =
          Object.keys(changes).length > 0 || client === null
            ? await updateClient(editingId, changes)
            : client;
      } else {
        savedClient = await createClient(full);
        // From here the client is real. A later failure must not send the
        // recruiter back through create.
        setCreatedId(savedClient.id);
      }
    } catch (err) {
      showFailure(err, "We could not save that client just now.");
      setSaving(false);
      return;
    }

    try {
      await saveContacts(savedClient.id);
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : "the contact could not be saved";
      setError(
        `The client was saved, but a contact was not: ${detail} Fix the contact and save again — the client will not be added twice.`,
      );
      setSaving(false);
      return;
    }

    setSaving(false);
    onDone(savedClient);
  }

  return (
    <Dialog
      titleId="client-form-title"
      className="dlg-modal-wide"
      onClose={saving ? () => {} : () => onCancel(createdId)}
      title={client ? `Edit ${client.name}` : "Add client"}
    >
      {error && (
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 12 }}>
          {error}
        </p>
      )}

      <form onSubmit={submit} className="cand-form" style={{ marginTop: 16 }}>
        <Field label="Client name" full required>
          <input
            className="jo-search"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="Acme Logistics Pte Ltd"
            required
            autoFocus
          />
        </Field>

        <Field label="Mail domain" hint={domainError ?? undefined}>
          {/* No client-side free-provider list: the set lives in the server's
              settings, and a copy here would be a second place for it to be
              wrong. The server names the domain and says why when it refuses,
              and that sentence renders under this field. */}
          <input
            className="jo-search"
            value={form.email_domain}
            onChange={(e) => set("email_domain", e.target.value)}
            placeholder="acme.com"
            aria-invalid={domainError ? true : undefined}
          />
        </Field>
        <Field label="Website">
          <input
            className="jo-search"
            value={form.website}
            onChange={(e) => set("website", e.target.value)}
            placeholder="https://acmelogistics.com.sg"
          />
        </Field>
        <Field label="Phone">
          <input
            className="jo-search"
            value={form.phone}
            onChange={(e) => set("phone", e.target.value)}
            placeholder="+65 6221 3344"
          />
        </Field>
        <Field label="Fee percent">
          {/* No `min`/`max`: the bounds are the server's rule, and it says
              which field and which bound it refused. */}
          <input
            className="jo-search"
            type="number"
            step="any"
            value={form.fee_percent}
            onChange={(e) => set("fee_percent", e.target.value)}
            placeholder="18"
          />
        </Field>
        <Field label="Payment terms (days)">
          <input
            className="jo-search"
            type="number"
            value={form.payment_terms_days}
            onChange={(e) => set("payment_terms_days", e.target.value)}
            placeholder="30"
          />
        </Field>
        <Field label="Address" full>
          <textarea
            className="jo-search"
            style={{ minHeight: 60 }}
            value={form.address}
            onChange={(e) => set("address", e.target.value)}
            placeholder="8 Tuas Avenue 10, Singapore 639145"
          />
        </Field>
        <Field label="Notes" full>
          <textarea
            className="jo-search"
            style={{ minHeight: 80 }}
            value={form.notes}
            onChange={(e) => set("notes", e.target.value)}
            placeholder="Prefers WhatsApp updates; invoices to accounts@acmelogistics.com.sg"
          />
        </Field>

        <fieldset className="cand-group cf-contacts">
          <legend className="row-k">Contacts</legend>
          <p className="body jo-sub cand-group-note">
            The people you deal with at this company. One can be marked primary — the one to call
            first. Leave a row completely blank and it is ignored.
          </p>
          {rows.length === 0 && (
            <p className="body muted">No contacts recorded.</p>
          )}
          {rows.map((row) => (
            <div key={row.key} className="cf-contact">
              <Field label="Name" required>
                <input
                  className="jo-search"
                  value={row.name}
                  onChange={(e) => setRow(row.key, { name: e.target.value })}
                  placeholder="Mei Ling Tan"
                  aria-invalid={!isBlankRow(row) && row.name.trim() === "" ? true : undefined}
                />
              </Field>
              <Field label="Title">
                <input
                  className="jo-search"
                  value={row.title}
                  onChange={(e) => setRow(row.key, { title: e.target.value })}
                  placeholder="HR Manager"
                />
              </Field>
              <Field label="Email">
                <input
                  className="jo-search"
                  type="email"
                  value={row.email}
                  onChange={(e) => setRow(row.key, { email: e.target.value })}
                  placeholder="meiling@acmelogistics.com.sg"
                />
              </Field>
              <Field label="Phone">
                <input
                  className="jo-search"
                  value={row.phone}
                  onChange={(e) => setRow(row.key, { phone: e.target.value })}
                  placeholder="+65 9123 4567"
                />
              </Field>
              <div className="cf-contact-acts">
                <label className="body cf-primary">
                  <input
                    type="radio"
                    // One name across the group is what makes these a single
                    // choice to a keyboard and a screen reader, not four
                    // unrelated radios.
                    name="client-primary-contact"
                    checked={row.is_primary}
                    onChange={() => choosePrimary(row.key)}
                  />{" "}
                  Primary contact
                </label>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => removeRow(row.key)}
                  disabled={saving}
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
          <div style={{ marginTop: 10 }}>
            <button type="button" className="btn btn-secondary" onClick={addRow} disabled={saving}>
              Add contact
            </button>
          </div>
          {namelessRow && (
            <p className="body jo-detail-error" role="alert">
              A contact needs a name. Fill it in, or clear the row entirely to drop it.
            </p>
          )}
        </fieldset>

        <div className="cand-form-full" style={{ display: "flex", gap: 10, marginTop: 8 }}>
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {saving ? "Saving…" : client || createdId ? "Save changes" : "Add client"}
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => onCancel(createdId)}
            disabled={saving}
          >
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function Field({
  label,
  required = false,
  full = false,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  /** Spans both columns — for the fields a half row misrepresents. */
  full?: boolean;
  /** Rendered under the control, as an alert. This is where a server
   *  refusal about this one field lands. Inside the `<label>` on purpose: it
   *  becomes part of the field's accessible name, so a screen reader user
   *  returning to the input later still hears why it was refused, rather than
   *  only at the moment the alert appeared. */
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className={full ? "cand-form-full" : undefined} style={{ display: "grid", gap: 4 }}>
      <span className="row-k">
        {label}
        {required && " *"}
      </span>
      {children}
      {hint && (
        <span className="body jo-detail-error" role="alert">
          {hint}
        </span>
      )}
    </label>
  );
}
