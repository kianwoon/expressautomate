"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CANDIDATE_IMPORTS_LIMIT,
  CANDIDATE_IMPORTS_PATH,
  CANDIDATES_PAGE_SIZE,
  CANDIDATES_PATH,
  candidateActivitiesPath,
  candidateArchivePath,
  candidateAvatarPath,
  candidateDocumentDownloadPath,
  candidateDocumentPath,
  candidateDocumentsPath,
  candidateImportErrorsPath,
  candidateImportUndoPath,
  candidateMergePath,
  candidatePath,
  candidateRestorePath,
  candidateRoleConfirmPath,
  candidateRolePath,
  candidateRoleRejectPath,
  candidateRolesPath,
  candidateUnmergePath,
  candidateWhatsappDraftPath,
  candidateWhatsappSendPath,
  WA_SESSION_PATH,
} from "../api";

/**
 * The agency's candidate list, and the one place that talks to the
 * candidates endpoint.
 *
 * Nothing here is AI-derived (§ candidate matching design) — every value was
 * typed by a person or came from a spreadsheet a person uploaded, so there is
 * no confidence and no evidence, only the record and who last edited it.
 *
 * Follows the fetch and typing pattern in `opportunities.ts`: `credentials:
 * "include"`, an `Accept: application/json` header, and `URLSearchParams` for
 * the query string.
 */

export type Stage = "new" | "contacted" | "submitted" | "placed" | "rejected";

/** How much of a date the source actually stated. A CV that says "2019" has
 *  no month in it, and rendering "1 Jan 2019" would assert a day nobody
 *  wrote down — the §15 rule this whole feature is built around. The
 *  precision travels with the date so the reader can only ever be shown what
 *  the source supports. */
export type DatePrecision = "year" | "month" | "day";

/** One job the candidate held.
 *
 * `ended_on: null` is what makes a role current, and `is_current` is the
 * server saying so rather than a second stored fact that could disagree with
 * the dates beside it.
 *
 * `source` and `status` have one useful value each today — a recruiter typed
 * it, and it is confirmed. They ship now because the CV parser that will
 * write `unconfirmed` rows is the next piece of work, and adding the columns
 * later would mean a migration and a redesign rather than an endpoint. */
export type CandidateRole = {
  id: string;
  employer: string;
  employer_normalized: string;
  title: string;
  title_normalized: string;
  started_on: string | null;
  started_precision: DatePrecision | null;
  ended_on: string | null;
  ended_precision: DatePrecision | null;
  employment_type: string | null;
  location: string | null;
  description: string | null;
  source: string;
  status: "unconfirmed" | "confirmed" | "rejected";
  is_current: boolean;
  /** The line of the CV the model quoted for this role. Optional rather than
   *  always-present: a role a recruiter typed by hand has no evidence row,
   *  and the panel falls back to offering the source file itself in that
   *  case. */
  evidence?: string | null;
};

/** How far the reading of one uploaded CV has got.
 *
 * These are not five flavours of failure and they must not read as one.
 * `unreadable` is permanent and has a cause a recruiter can act on — a scan
 * carries no text layer. `empty` is a file we read fine that yielded nothing
 * that survived checking. `failed` is transient and worth retrying. The
 * difference between them is the whole difference between a product that is
 * broken and one that is telling you what happened. */
export type ParseState = "pending" | "parsing" | "parsed" | "unreadable" | "empty" | "failed";

/** One CV uploaded against a candidate.
 *
 * `dropped_count` and `dropped_reason` stay set on a *successful* parse: they
 * are the note that says the model proposed things whose quoted text could not
 * be found in the document and they were thrown away. Without it a recruiter
 * whose CV lists five jobs and whose panel shows three concludes the parser
 * cannot count. */
export type CandidateDocument = {
  id: string;
  filename: string;
  content_type: string;
  byte_size: number;
  parse_state: ParseState;
  parse_error: string | null;
  text_chars: number | null;
  dropped_count: number | null;
  dropped_reason: string | null;
  created_at: string | null;
};

/** What a create sends, and — one key at a time — what a patch sends. */
export type CandidateRoleBody = {
  employer?: string;
  title?: string;
  started_on?: string | null;
  started_precision?: DatePrecision | null;
  ended_on?: string | null;
  ended_precision?: DatePrecision | null;
  employment_type?: string | null;
  location?: string | null;
  description?: string | null;
};

export type Candidate = {
  id: string;
  full_name: string;
  email: string | null;
  phone_raw: string | null;
  /** The number normalised to E.164, or `null` when nothing WhatsApp can
   *  reach — deliberately left unset for landlines, so `null` here means the
   *  WhatsApp button has nowhere to send to, not merely that no phone was
   *  recorded (`phone_raw` covers that case). */
  phone_e164: string | null;
  current_title: string | null;
  current_employer: string | null;
  location: string | null;
  years_experience: number | null;
  expected_salary: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  available_from: string | null;
  notice_period_raw: string | null;
  employment_type: string | null;
  notes: string | null;
  pipeline_stage: Stage;
  record_status: "active" | "archived" | "merged";
  updated_at: string;
  merged_into_candidate_id?: string | null;
  /** Only present on the single-record GET, not on a list row. */
  skills?: string[];
  /** Only present on the single-record GET, not on a list row. Optional
   *  rather than defaulted to `[]`, because a list row has not been asked
   *  about its roles — absent must read as "not loaded", never as "none".
   *  Already ordered current-first then newest by the server. */
  roles?: CandidateRole[];
  /** Only present on the single-record GET, not on a list row. Optional for
   *  the same reason `roles` is: a table row has never been asked about its
   *  uploads, and absent has to mean "not loaded" rather than "none" — the
   *  difference between saying nothing and asserting there is no CV.
   *  Newest first, as the server orders them. */
  documents?: CandidateDocument[];
  /** Only present on the single-record GET, not on a list row. */
  overridden_fields?: string[];
  /** Set once a photo has been uploaded; `null` means "show the initials
   *  fallback" rather than "loading". Never used to build a URL directly —
   *  the URL itself is a separate, short-lived fetch (see `getCandidateAvatar`). */
  avatar_key: string | null;
  avatar_updated_at: string | null;
};

export type CandidatePage = {
  items: Candidate[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
  /** Which first letters currently have anyone behind them, sorted, `#` last.
   *  The server computes it with the stage, status and search filters applied
   *  but *without* `initial`, so picking a letter can never empty the bar that
   *  was used to pick it. */
  initials: string[];
};

/** The chips. `null` is "All" (every non-merged record). `"merged"` is its
 *  own value rather than a `Stage` — merged rows have no pipeline stage
 *  worth filtering on, and reaching them is a `record_status` query, not a
 *  `pipeline_stage` one. */
export type Filter = null | Stage | "merged";

function listUrl(filter: Filter, offset: number, q: string, initial: string | null): string {
  const params = new URLSearchParams({
    limit: String(CANDIDATES_PAGE_SIZE),
    offset: String(offset),
  });
  if (filter === "merged") params.set("record_status", "merged");
  else if (filter) params.set("pipeline_stage", filter);
  if (q.trim()) params.set("q", q.trim());
  if (initial) params.set("initial", initial);
  return `${CANDIDATES_PATH}?${params.toString()}`;
}

function messageFor(status: number): string {
  // A 401 is our session expiring, not the candidate list failing. Saying "we
  // could not load your candidates" for an expired cookie sends someone to
  // look at the wrong thing entirely.
  return status === 401
    ? "Your session has expired. Sign in again to see your candidates."
    : "We could not load your candidates just now.";
}

export type ListState =
  | { status: "loading" }
  | { status: "ready"; page: CandidatePage }
  | { status: "unreadable"; message: string };

const ZERO_COUNTS: Record<string, number> = { all: 0 };
const NO_INITIALS: string[] = [];

export type Candidates = {
  state: ListState;
  filter: Filter;
  offset: number;
  q: string;
  /** The letter the list is narrowed to, or `null` for all of them. */
  initial: string | null;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. */
  counts: Record<string, number>;
  /** The last letters we were told, kept across a reload for the same reason
   *  as `counts`: an index bar that greys out entirely on every page change is
   *  a bar nobody can aim at. */
  initials: string[];
  /** A refetch is in flight over rows we are still showing. The same reasoning
   *  as `counts`, applied to the table: a reload that dropped `state` back to
   *  `loading` would unmount the table and the open detail panel, so archiving
   *  a row or uploading a photo read as the whole page going blank. The
   *  in-flight fact is reported here instead, and only the genuine first load —
   *  when there is nothing on screen yet — is a `loading` state. */
  refreshing: boolean;
  setFilter: (filter: Filter) => void;
  setOffset: (offset: number) => void;
  setQ: (q: string) => void;
  setInitial: (initial: string | null) => void;
  reload: () => void;
};

export function useCandidates(): Candidates {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>(null);
  const [offset, setOffset] = useState(0);
  const [q, setQRaw] = useState("");
  const [initial, setInitialRaw] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>(ZERO_COUNTS);
  const [initials, setInitials] = useState<string[]>(NO_INITIALS);
  const [refreshing, setRefreshing] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    // Rows already on screen stay on screen while the refetch runs, exactly as
    // the counts do — a page that is being re-read is not a page with nothing
    // in it, and collapsing to `loading` here is what made an avatar upload,
    // an archive or a merge look like the whole screen had reloaded.
    setState((prev) => (prev.status === "ready" ? prev : { status: "loading" }));
    setRefreshing(true);
    (async () => {
      try {
        const res = await fetch(listUrl(filter, offset, q, initial), {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({ status: "unreadable", message: messageFor(res.status) });
          return;
        }
        const page = (await res.json()) as CandidatePage;
        setState({ status: "ready", page });
        setCounts(page.counts);
        setInitials(page.initials);
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      } finally {
        // An aborted request has been superseded by the next one, which has
        // already set this back to true; clearing it here would report idle
        // while a fetch is still running.
        if (!controller.signal.aborted) setRefreshing(false);
      }
    })();
    return () => controller.abort();
  }, [filter, offset, q, initial, nonce]);

  // Changing the filter or the search must reset the page, for the same
  // reason as job orders: staying on offset 150 of five matching rows reads
  // exactly like "there are none".
  const setFilter = useCallback((next: Filter) => {
    setFilterRaw(next);
    setOffset(0);
  }, []);
  const setQ = useCallback((next: string) => {
    setQRaw(next);
    setOffset(0);
  }, []);
  // A letter is a filter like any other, so it resets the page for the reason
  // above.
  const setInitial = useCallback((next: string | null) => {
    setInitialRaw(next);
    setOffset(0);
  }, []);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return {
    state,
    filter,
    offset,
    q,
    initial,
    counts,
    initials,
    refreshing,
    setFilter,
    setOffset,
    setQ,
    setInitial,
    reload,
  };
}

/** Turns a non-2xx response into the server's own message where it gave one,
 *  rather than a generic failure. A 409 here is a split identity or a key
 *  someone else already holds — the message is the only thing telling the
 *  recruiter what to do next. */
export async function readError(res: Response): Promise<string> {
  if (res.status === 401) return "Your session has expired. Sign in again, then try that once more.";
  try {
    const body = (await res.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    /* not JSON, or empty */
  }
  return "We could not save that just now. Nothing has changed.";
}

export class ApiError extends Error {}

export async function getCandidate(id: string): Promise<Candidate> {
  const res = await fetch(candidatePath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Candidate;
}

export async function createCandidate(
  body: Partial<Candidate> & { full_name: string },
): Promise<Candidate> {
  const res = await fetch(CANDIDATES_PATH, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Candidate;
}

export async function updateCandidate(id: string, body: Partial<Candidate>): Promise<Candidate> {
  const res = await fetch(candidatePath(id), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Candidate;
}

export async function archiveCandidate(id: string): Promise<void> {
  const res = await fetch(candidateArchivePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function restoreCandidate(id: string): Promise<void> {
  const res = await fetch(candidateRestorePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function deleteCandidate(id: string): Promise<void> {
  const res = await fetch(candidatePath(id), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function mergeCandidate(id: string, targetId: string): Promise<void> {
  const res = await fetch(candidateMergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ target_id: targetId }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function unmergeCandidate(id: string): Promise<void> {
  const res = await fetch(candidateUnmergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function createCandidateRole(
  id: string,
  body: CandidateRoleBody & { employer: string; title: string },
): Promise<CandidateRole> {
  const res = await fetch(candidateRolesPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

/** A true partial update: only the keys present in `body` are touched, and an
 *  explicit `null` clears a nullable field. Callers must therefore send the
 *  keys they mean and omit the rest — spreading a whole role in here would
 *  turn every edit into a replacement. */
export async function updateCandidateRole(
  id: string,
  roleId: string,
  body: CandidateRoleBody,
): Promise<CandidateRole> {
  const res = await fetch(candidateRolePath(id, roleId), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

export async function deleteCandidateRole(id: string, roleId: string): Promise<void> {
  const res = await fetch(candidateRolePath(id, roleId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** A person vouches for what the parse read off the CV. Returns the updated
 *  role; the caller still refetches the candidate, because confirming can move
 *  the derived title, employer and years above. */
export async function confirmCandidateRole(id: string, roleId: string): Promise<CandidateRole> {
  const res = await fetch(candidateRoleConfirmPath(id, roleId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

/** A person says the parse got this one wrong. The row is kept rather than
 *  deleted — it is the record of what the model claimed, and a re-parse of the
 *  same CV must not resurrect something already thrown out. */
export async function rejectCandidateRole(id: string, roleId: string): Promise<CandidateRole> {
  const res = await fetch(candidateRoleRejectPath(id, roleId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

/** Uploads a CV. 202, not 201: the file is stored and queued, and the reading
 *  of it happens afterwards — which is why the document comes back in a
 *  `pending` state rather than with roles attached. */
export async function uploadCandidateDocument(
  id: string,
  file: File,
): Promise<CandidateDocument> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(candidateDocumentsPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  // Oversized, wrong type and over-quota all arrive as a readable sentence the
  // server wrote; `readError` surfaces it rather than replacing it with a
  // guess about which of the three it was.
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateDocument;
}

export async function deleteCandidateDocument(id: string, documentId: string): Promise<void> {
  const res = await fetch(candidateDocumentPath(id, documentId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export type DocumentUrl = { url: string; expires_in: number };

/** A short-lived presigned URL for the original file. Fetched at the moment
 *  the recruiter asks for it and never held: the link stops working within
 *  minutes, so anything longer-lived than the click is a broken link waiting
 *  to be found. */
export async function getCandidateDocumentUrl(
  id: string,
  documentId: string,
): Promise<DocumentUrl> {
  const res = await fetch(candidateDocumentDownloadPath(id, documentId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as DocumentUrl;
}

export type AvatarUrl = { url: string; expires_in: number };

/** A presigned URL good for roughly `expires_in` seconds — `null` means the
 *  candidate has no photo (a 404), not a failed request. Callers must not
 *  hold onto the URL past the component's lifetime: it is re-fetched every
 *  time the panel opens rather than cached anywhere longer-lived. */
export async function getCandidateAvatar(id: string): Promise<AvatarUrl | null> {
  const res = await fetch(candidateAvatarPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as AvatarUrl;
}

export async function uploadCandidateAvatar(
  id: string,
  file: File,
): Promise<{ avatar_key: string; avatar_updated_at: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(candidateAvatarPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as { avatar_key: string; avatar_updated_at: string };
}

/** Which sheet a CSV is standing in for.
 *
 *  A CSV is one nameless sheet, so the uploader has to say which of the two it
 *  holds; the server decides from this field and never from the filename, so a
 *  file called `history.csv` full of candidates is still a candidate sheet if
 *  that is what was chosen. An XLSX carries both sheets by name and the field
 *  is ignored — which is why the picker stays on screen either way rather than
 *  guessing from an extension we do not trust. */
export type ImportSheet = "Candidates" | "History";

/** How far one uploaded spreadsheet has got.
 *
 * `failed` is the whole run falling over — a file we could not read at all, or
 * one that never reached the queue. It is not the same as a `done` import with
 * `rows_failed` above zero, which is the ordinary case of a sheet where most
 * rows landed and a few did not. `undone` is the only state that was reached
 * by someone choosing it. */
export type ImportState = "pending" | "parsing" | "done" | "failed" | "undone";

export type CandidateImport = {
  id: string;
  filename: string;
  content_type: string;
  byte_size: number;
  state: ImportState;
  candidates_created: number;
  candidates_updated: number;
  roles_created: number;
  roles_updated: number;
  rows_failed: number;
  /** Whether a report was actually written, which is not the same question as
   *  `rows_failed > 0`: a run that fell over before it read a row has no failed
   *  rows and still has a report saying why. The link follows this, not the
   *  count. */
  has_errors: boolean;
  created_at: string | null;
};

/** One field, or one whole candidate, that undo left alone — and why.
 *
 * `field_name` is `"*"` when the skip is about the row rather than a column:
 * a candidate the import created but which has since had a role added to it by
 * hand is kept whole, because deleting it would take that work with it. */
export type UndoSkip = {
  entity_type: string;
  entity_id: string;
  field_name: string;
  reason: string;
};

/** What an undo actually managed.
 *
 * `already_undone` is the server short-circuiting a second undo rather than
 * running one: the counts and skips are absent, because re-running would report
 * its own first pass's work as a page of skips. Callers must branch on it
 * before reading anything else — presenting that as "we protected your changes"
 * would be a lie in the one place a person is checking whether their data is
 * safe. */
export type UndoResult = {
  import: CandidateImport;
  already_undone: boolean;
  rows_deleted?: number;
  fields_restored?: number;
  fields_skipped?: number;
  skips?: UndoSkip[];
};

/** Recent imports, newest first.
 *
 * The limit is always sent. The endpoint has none of its own, and an agency
 * with three years of imports behind it would otherwise fetch all of them to
 * draw a table of the last few. */
export async function listCandidateImports(): Promise<CandidateImport[]> {
  const params = new URLSearchParams({ limit: String(CANDIDATE_IMPORTS_LIMIT) });
  const res = await fetch(`${CANDIDATE_IMPORTS_PATH}?${params.toString()}`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateImport[];
}

/** Uploads a spreadsheet. 202, not 201: the row exists and the answer does
 *  not — the rows are read by a job afterwards, which is why what comes back is
 *  `pending` with every count at zero. */
export async function uploadCandidateImport(
  file: File,
  sheet: ImportSheet,
): Promise<CandidateImport> {
  const form = new FormData();
  form.append("file", file);
  form.append("sheet", sheet);
  const res = await fetch(CANDIDATE_IMPORTS_PATH, {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  // Oversized (413), not a spreadsheet at all (415) and an unnamed sheet (422)
  // each arrive as a sentence the server wrote; `readError` surfaces it rather
  // than replacing it with a guess about which of the three it was.
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateImport;
}

/** A short-lived URL for the row-by-row error report, fetched at the moment it
 *  is asked for and never held — the same bargain the CV download keeps, and
 *  for a stronger reason: this report names candidates. */
export async function getCandidateImportErrorsUrl(importId: string): Promise<DocumentUrl> {
  const res = await fetch(candidateImportErrorsPath(importId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as DocumentUrl;
}

/** What the draft endpoint hands back: the number already in the shape the
 *  WhatsApp URL wants (plus its sign) and a message ready to edit. */
export type WhatsappDraft = { phone_e164: string; message: string };

/** A 409 here means the candidate has no `phone_e164` — the server's own
 *  `detail` says so in words a recruiter can act on, so callers should show
 *  it verbatim rather than a generic failure. */
export async function getWhatsappDraft(id: string): Promise<WhatsappDraft> {
  const res = await fetch(candidateWhatsappDraftPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as WhatsappDraft;
}

export type ActivityItem = {
  id: string;
  activity_type: string;
  channel: string;
  message_text: string | null;
  status: string;
  actor_name: string;
  created_at: string;
};

/** The WA gateway session status, mirrored from `whatsapp-panel.tsx`'s wire
 *  vocabulary (the database CHECK constraint plus the API-only
 *  `gateway_unreachable`). Duplicated here rather than imported: that panel's
 *  type is a `SessionResponse` shaped for its own richer QR/expiry fields, and
 *  the draft modal only ever needs the bare status string. */
export type WaSessionStatus =
  | "pairing"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "logged_out"
  | "gateway_unreachable";

/** Whether the recruiter's own WhatsApp is linked right now — just enough to
 *  decide whether the draft modal can offer `Send` as well as `Open
 *  WhatsApp`. `gateway_unreachable` covers both a non-2xx and a network
 *  failure, same as the settings panel: "we cannot tell", never "you are
 *  disconnected". */
export async function getWaSessionStatus(): Promise<WaSessionStatus> {
  try {
    const res = await fetch(WA_SESSION_PATH, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return "gateway_unreachable";
    const body = (await res.json()) as { status: WaSessionStatus };
    return body.status;
  } catch {
    return "gateway_unreachable";
  }
}

/** What a send either returns (200) or throws as (409/429/422, via
 *  `WhatsappSendError`). */
/** The 200 body. `status` is load-bearing and was missing here: a send that
 *  reached WhatsApp and one whose answer never came back are both 200s, and
 *  only this field tells them apart. `unknown` is not a success — the caller
 *  must not close the modal on it. `provider_message_id` is null unless
 *  WhatsApp actually handed one back. */
export type WhatsappSendResult = {
  status: "sent" | "unknown" | "failed";
  activity_id: string;
  provider_message_id: string | null;
  client_request_id: string;
};

/** A 409/429/422 is not a generic failure — it carries the server's own
 *  sentence, so the modal can say something a recruiter can act on. `kind`
 *  distinguishes the three because each has a different fix: `session` (409,
 *  `session_status` set) means link or wait; `rate_limited` (429) means the
 *  daily cap is hit — the popup is still open to them; `no_number` (422)
 *  means the fix is on the candidate record, not in Settings → WhatsApp, so
 *  the modal must not point there. */
export class WhatsappSendError extends ApiError {
  kind: "session" | "rate_limited" | "no_number";
  session_status: string | null;
  constructor(
    message: string,
    kind: "session" | "rate_limited" | "no_number",
    session_status: string | null,
  ) {
    super(message);
    this.kind = kind;
    this.session_status = session_status;
  }
}

/** Sends the draft through the recruiter's own linked WhatsApp session.
 *
 * `client_request_id` is generated once per composed draft (by the caller,
 * on modal open or on edit) and resent unchanged on every retry of that same
 * message — the server unique-indexes it, so a client-side timeout that hits
 * after the gateway already accepted the send turns a recruiter's retry into
 * a no-op instead of a second message to the candidate.
 *
 * The server logs the activity itself on success — unlike
 * `logCandidateActivity` above, which the popup path calls because the
 * browser, not the server, is what knows the popup opened. */
export async function sendCandidateWhatsapp(
  id: string,
  message: string,
  clientRequestId: string,
): Promise<WhatsappSendResult> {
  const res = await fetch(candidateWhatsappSendPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ message, client_request_id: clientRequestId }),
  });
  if (res.status === 409 || res.status === 429 || res.status === 422) {
    let detail = "We could not send that just now.";
    let session_status: string | null = null;
    try {
      const body = (await res.json()) as { detail?: string; session_status?: string };
      if (body.detail) detail = body.detail;
      if (body.session_status) session_status = body.session_status;
    } catch {
      /* not JSON, or empty */
    }
    const kind = res.status === 429 ? "rate_limited" : res.status === 422 ? "no_number" : "session";
    throw new WhatsappSendError(detail, kind, session_status);
  }
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as WhatsappSendResult;
}

export type ActivityBody = { activity_type: string; channel: string; message_text: string };

/** Records that WhatsApp was *opened* — never that a message was sent, since
 *  the recruiter presses send themselves inside WhatsApp and this app never
 *  observes that. */
export async function logCandidateActivity(id: string, body: ActivityBody): Promise<void> {
  const res = await fetch(candidateActivitiesPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function getCandidateActivities(id: string): Promise<ActivityItem[]> {
  const res = await fetch(candidateActivitiesPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { items: ActivityItem[] };
  return body.items;
}

/** Walks one import back as far as it is still safe to.
 *
 * A 409 here is the run still parsing, and the server's own sentence says to
 * wait — which is why it travels through `readError` untouched rather than
 * becoming a generic failure. */
export async function undoCandidateImport(importId: string): Promise<UndoResult> {
  const res = await fetch(candidateImportUndoPath(importId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as UndoResult;
}

export async function deleteCandidateAvatar(id: string): Promise<void> {
  const res = await fetch(candidateAvatarPath(id), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}
