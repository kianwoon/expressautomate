"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CANDIDATE_ACCESS_REQUESTS_PATH,
  CANDIDATE_IMPORTS_LIMIT,
  CANDIDATE_IMPORTS_PATH,
  CANDIDATES_DOCUMENTS_PATH,
  CANDIDATES_PAGE_SIZE,
  CANDIDATES_PAGE_SIZES,
  CANDIDATES_PATH,
  candidateAccessRequestDeclinePath,
  candidateAccessRequestGrantPath,
  candidateAccessRequestsPath,
  candidateActivitiesPath,
  candidateArchivePath,
  candidateAssignPath,
  candidateAvatarPath,
  candidateClaimPath,
  candidateSharePath,
  candidateSharesPath,
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
  candidateWhatsappTranslatePath,
  WA_SESSION_PATH,
} from "../api";
import { cacheKey, cachedSignedUrl, forget } from "./signed-url-cache";
import {
  CANDIDATES_PAGE_SIZE_KEY,
  usePersistedPageSize,
} from "./use-persisted-page-size";
import type { CandidateSort } from "./candidates/candidates-table";

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
export type ParseState =
  | "ingest_pending"
  | "ingesting"
  | "pending"
  | "parsing"
  | "parsed"
  | "unreadable"
  | "empty"
  | "failed"
  | "needs_review";

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
  last_drawn_salary: number | null;
  last_drawn_currency: string | null;
  last_drawn_period: string | null;
  available_from: string | null;
  notice_period_raw: string | null;
  employment_type: string | null;
  notes: string | null;
  pipeline_stage: Stage;
  record_status: "active" | "archived" | "merged";
  updated_at: string;
  /**
   * The recruiter who holds this candidate, resolved server-side —
   * `null` for the unclaimed queue.
   *
   * `name` is `_OWNER_NAME` in `backend/app/api/candidates.py` — preferred
   * name, then display name, then the email signed in with — the same
   * coalesce `held_by_colleague` uses for the 409. Read it as given; do not
   * resolve it a second time through `useMembers()`, which is a second place
   * for the name to disagree with the server's.
   */
  owner: { id: string; name: string | null } | null;
  /**
   * Whether THIS reader may edit THIS candidate — `can_edit_candidate` on the
   * server (the owner, or an agency owner), published rather than
   * re-derived. Every candidate payload carries it since 2c6051f, on both
   * the list and the single-record read. See `canEditCandidate` below for
   * why a caller must read this field and nothing else.
   */
  can_edit: boolean;
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
  /** The four facts a Singapore MDW Work Permit turns on, and the two that
   *  describe race. All single-record-only, and deliberately so: the server
   *  keeps them off list rows because a screen showing everyone's sex and
   *  race side by side is a screen that invites shortlisting by eye.
   *
   *  `null` is a first-class value here, not a gap to be filled in. An
   *  unrecorded date of birth makes the age check `unknown`, which *keeps* a
   *  candidate in the eligible list — only a fact that is known and fails
   *  removes anyone. So nothing in the UI may treat absent as a defect, and
   *  nothing may guess. */
  sex?: "female" | "male" | null;
  race?: "chinese" | "malay" | "indian" | "others" | null;
  race_detail?: string | null;
  nationality?: string | null;
  date_of_birth?: string | null;
  education_years?: number | null;
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
  /** `null` under `eligible_for`: the server has no honest per-stage number
   *  for a filtered list, so it sends none rather than one describing a wider
   *  population than the rows beside it. Draw nothing, never a stale figure. */
  counts: Record<string, number> | null;
  /** Which first letters currently have anyone behind them, sorted, `#` last.
   *  The server computes it with the stage, status and search filters applied
   *  but *without* `initial`, so picking a letter can never empty the bar that
   *  was used to pick it. `null` under `eligible_for`, for the same reason as
   *  `counts`. */
  initials: string[] | null;
  /** Present only when the request carried `eligible_for` — how many rows the
   *  regulatory check removed. Absent, not zero, when the filter is off: a
   *  page that was never filtered has nothing to report having excluded. */
  excluded_ineligible?: number;
  /** Present only alongside `excluded_ineligible` — how many candidates were
   *  actually evaluated. When `scan_truncated` is true this is smaller than
   *  the agency's whole candidate count, and `excluded_ineligible` must be
   *  read against it, not against the total. */
  scanned?: number;
  /** True when the scan hit its ceiling and stopped before evaluating every
   *  candidate. The list is then eligible people *from the first `scanned`
   *  examined* — there may be more beyond it that were never looked at. */
  scan_truncated?: boolean;
};

/** The chips. `null` is "All" (every non-merged record). `"merged"` is its
 *  own value rather than a `Stage` — merged rows have no pipeline stage
 *  worth filtering on, and reaching them is a `record_status` query, not a
 *  `pipeline_stage` one. */
export type Filter = null | Stage | "merged";

/**
 * Whose candidates, which is a different question from what stage they are at.
 *
 * The same four values the job order list uses, and deliberately the same
 * words: a recruiter who has learnt "Queue" on one screen must not have to
 * learn a second vocabulary on the other. `all` is the server's default and
 * the only one that is not sent — see `listUrl`.
 *
 * It is also what makes "all" honest. `all` was never everything; it was
 * always "everything I am allowed to see", and without the other three
 * beside it there is nothing on the screen saying so.
 */
export type Scope = "mine" | "queue" | "shared_with_me" | "all";

function listUrl(
  filter: Filter,
  offset: number,
  q: string,
  initial: string | null,
  eligibleFor: string | null,
  scope: Scope,
  limit: number,
  sort: CandidateSort,
): string {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (filter === "merged") params.set("record_status", "merged");
  else if (filter) params.set("pipeline_stage", filter);
  if (q.trim()) params.set("q", q.trim());
  if (initial) params.set("initial", initial);
  if (eligibleFor) params.set("eligible_for", eligibleFor);
  // Omitted at `all`, exactly as `opportunities.ts` omits it: `all` is the
  // server's own default, and sending it would put a redundant parameter on
  // every ordinary request.
  if (scope !== "all") params.set("scope", scope);
  // The sort travels on every request. The server ignores it when an A–Z
  // initial is active (the index pins the list to that letter by name), so
  // sending it is harmless there and correct everywhere else.
  params.set("sort", sort.key);
  params.set("descending", String(sort.descending));
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
  | { status: "unreadable"; message: string }
  /** The job order carried by `eligible_for` has no `placement_type` set —
   *  the server's 409. Its own state rather than folded into `unreadable`,
   *  because the fix is not "reload the page", it is "set the placement
   *  type", and the caller needs `reason` to know that without parsing
   *  `detail`. */
  | { status: "blocked"; detail: string; reason: string };

const ZERO_COUNTS: Record<string, number> = { all: 0 };
const NO_INITIALS: string[] = [];

export type Candidates = {
  state: ListState;
  filter: Filter;
  offset: number;
  q: string;
  /** The letter the list is narrowed to, or `null` for all of them. */
  initial: string | null;
  /** The opportunity id the list is narrowed to, or `null` for the ordinary,
   *  unfiltered list. Carried in from the job order's "Find candidates for
   *  this role" action via the URL, never from a picker on this page. */
  eligibleFor: string | null;
  /** Whose candidates the list is narrowed to. A second axis, independent of
   *  `filter`: "mine, at the submitted stage" is one question, and a recruiter
   *  must not have to choose which half of it to ask. */
  scope: Scope;
  /** The column + direction the list is sorted by. Default `{ key: "updated",
   *  descending: true }` — the fixed order the list had before sorting landed. */
  sort: CandidateSort;
  /** What is being asked for — distinct from `state.page.limit`, which is what
   *  the server actually used and clamps. Mirrors `limit`/`OPPORTUNITIES_PAGE_SIZE`
   *  in `opportunities.ts`: the select the page-size control renders must show
   *  what was asked, because a select whose value disagrees with its options
   *  renders blank. */
  limit: number;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. `null` while the
   *  eligibility filter is on — and the stickiness is exactly why that has to
   *  travel as a real `null` rather than as "leave what was there": holding
   *  the previous figures would put the unfiltered totals above the filtered
   *  rows, which is the misreading this is here to prevent. */
  counts: Record<string, number> | null;
  /** The last letters we were told, kept across a reload for the same reason
   *  as `counts`: an index bar that greys out entirely on every page change is
   *  a bar nobody can aim at. `null` while filtering, likewise. */
  initials: string[] | null;
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
  setEligibleFor: (eligibleFor: string | null) => void;
  setScope: (scope: Scope) => void;
  /** Changes the sort column/direction. Resets to the first page, exactly as
   *  every other narrowing control does: offset 150 of a 10-row page is page
   *  sixteen, and re-sorting deep in a list lands on rows that no longer follow
   *  one another. */
  setSort: (sort: CandidateSort) => void;
  /** Back to the first page for the same reason `setScope` does: offset 150
   *  of a 10-row page is page sixteen, and of a 50-row page it is past the end
   *  of a 200-row list. Growing the page while standing deep in the list would
   *  land on nothing. */
  setLimit: (limit: number) => void;
  reload: () => void;
};

/** `initialEligibleFor` seeds the filter from the URL the candidates page was
 *  navigated to with — read once by the caller, on mount, never re-derived
 *  here, so a later edit to the address bar cannot silently retarget an
 *  already-open list. */
export function useCandidates(initialEligibleFor: string | null = null): Candidates {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>(null);
  const [offset, setOffset] = useState(0);
  const [q, setQRaw] = useState("");
  const [initial, setInitialRaw] = useState<string | null>(null);
  const [eligibleFor, setEligibleForRaw] = useState<string | null>(initialEligibleFor);
  const [limit, setLimitRaw] = usePersistedPageSize(
    CANDIDATES_PAGE_SIZE_KEY,
    CANDIDATES_PAGE_SIZE,
    CANDIDATES_PAGE_SIZES,
  );
  const [counts, setCounts] = useState<Record<string, number> | null>(ZERO_COUNTS);
  const [initials, setInitials] = useState<string[] | null>(NO_INITIALS);
  const [scope, setScopeRaw] = useState<Scope>("all");
  // The default `{ key: "updated", descending: true }` is the fixed order the
  // list had before sorting landed — most-recently-updated first — so a page
  // that has not touched a column header renders exactly as it always did.
  const [sort, setSort] = useState<CandidateSort>({
    key: "updated",
    descending: true,
  });
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
        const res = await fetch(
          listUrl(filter, offset, q, initial, eligibleFor, scope, limit, sort),
          {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (res.status === 409) {
          const body = (await res.json().catch(() => ({}))) as {
            detail?: string;
            reason?: string;
          };
          setState({
            status: "blocked",
            detail: body.detail ?? "This job order needs a placement type before it can be used this way.",
            reason: body.reason ?? "placement_type_not_set",
          });
          return;
        }
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
  }, [filter, offset, q, initial, eligibleFor, scope, limit, sort, nonce]);

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
  // Clearing or setting the eligibility filter is a filter change like any
  // other, so the page resets for the reason above. The caller is also
  // responsible for taking `eligible_for` out of the URL when clearing —
  // this hook only owns the fetch, not the address bar.
  const setEligibleFor = useCallback((next: string | null) => {
    setEligibleForRaw(next);
    setOffset(0);
  }, []);
  // Whose candidates is a filter like any other, so it resets the page for
  // the reason above — and more sharply than the others: "Mine" over a
  // tenant-wide offset is the case most likely to land on an empty page and
  // read as "you hold nobody".
  const setScope = useCallback((next: Scope) => {
    setScopeRaw(next);
    setOffset(0);
  }, []);
  // Re-sorting from deep in a list lands on rows that no longer follow one
  // another, so the offset resets — the same reason every other narrowing
  // control does.
  const setSortAndReset = useCallback((next: CandidateSort) => {
    setSort(next);
    setOffset(0);
  }, []);
  // A page-size change is a filter like any other for the purposes of the
  // offset, and more sharply than most: standing on offset 150 when the page
  // grows to 50 rows is standing past the end of a 200-row list.
  const setLimit = useCallback((next: number) => {
    setLimitRaw(next);
    setOffset(0);
  }, []);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return {
    state,
    filter,
    offset,
    q,
    initial,
    eligibleFor,
    scope,
    sort,
    setSort: setSortAndReset,
    limit,
    counts,
    initials,
    refreshing,
    setFilter,
    setOffset,
    setQ,
    setInitial,
    setEligibleFor,
    setScope,
    setLimit,
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
    const body = (await res.json()) as { detail?: unknown };
    // `typeof === "string"`, not merely truthy. FastAPI's `detail` is whatever
    // the route passed it, and the collision 409 passes an OBJECT — returned
    // from here it would be handed to React as the error text and thrown on
    // render. `collisionFrom` below is what reads that shape; anything else
    // structured falls through to the generic line rather than crashing the
    // form it was supposed to explain.
    if (typeof body.detail === "string" && body.detail) return body.detail;
  } catch {
    /* not JSON, or empty */
  }
  return "We could not save that just now. Nothing has changed.";
}

export class ApiError extends Error {}

/**
 * A candidate a colleague already holds.
 *
 * The abbreviated name is the server's, masked in `candidate_matching.py`, and
 * is rendered exactly as given: expanding "Wei Ming T." back to a full name is
 * the disclosure the masking exists to prevent, and prettifying it is guessing
 * at a real person's name.
 */
export type CandidateCollision = {
  reason: "already_registered";
  candidate: {
    full_name: string;
    held_by: string;
    /**
     * Sent by the server since 2c6051f — `held_by_colleague` in
     * `candidate_matching.py` now includes it deliberately: it is the one
     * thing `POST /candidates/{id}/access-requests` needs, and withholding
     * it left `can_request_access: true` a promise the caller could not act
     * on. Still read optionally rather than required: an older server
     * response (or a test fixture pinned to the old shape) sends none, and
     * the button falls back to disabled-with-a-reason rather than crashing.
     */
    id?: string | null;
  };
  can_request_access: boolean;
};

/** Thrown instead of a bare `ApiError` when a create or an edit collides with
 *  a candidate outside the caller's sight. Carries the body, because the whole
 *  point is that this is not a validation failure and must not be rendered as
 *  one — the value the recruiter typed is correct. */
export class CandidateCollisionError extends ApiError {
  readonly collision: CandidateCollision;

  constructor(collision: CandidateCollision) {
    super(`Already registered by ${collision.candidate.held_by}`);
    this.collision = collision;
  }
}

/** The collision body, or `null` for any other 409. Reads through FastAPI's
 *  `detail` wrapper, which is where `HTTPException(detail={...})` puts it. */
function collisionFrom(body: unknown): CandidateCollision | null {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail !== "object" || detail === null) return null;
  const shape = detail as Partial<CandidateCollision>;
  if (shape.reason !== "already_registered") return null;
  if (typeof shape.candidate?.full_name !== "string") return null;
  if (typeof shape.candidate?.held_by !== "string") return null;
  return {
    reason: "already_registered",
    candidate: {
      full_name: shape.candidate.full_name,
      held_by: shape.candidate.held_by,
      id: shape.candidate.id ?? null,
    },
    can_request_access: shape.can_request_access === true,
  };
}

/** Every non-2xx from a write, as the right kind of error.
 *
 * One function rather than a branch repeated at each call site: a create and
 * an edit can both collide, and the second copy of this is where the two
 * would drift on which one still renders a red field.
 */
async function writeError(res: Response): Promise<ApiError> {
  if (res.status === 409) {
    // Cloned, so `readError` below can still read the body if this is some
    // other 409 — a response body can only be consumed once.
    const parsed = await res
      .clone()
      .json()
      .catch(() => null);
    const collision = collisionFrom(parsed);
    if (collision) return new CandidateCollisionError(collision);
  }
  return new ApiError(await readError(res));
}

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
  if (!res.ok) throw await writeError(res);
  return (await res.json()) as Candidate;
}

export async function updateCandidate(id: string, body: Partial<Candidate>): Promise<Candidate> {
  const res = await fetch(candidatePath(id), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  // A PATCH collides too — changing an email onto a key a colleague holds is
  // the same event as creating one, and `patch_collision` returns the same
  // body for it.
  if (!res.ok) throw await writeError(res);
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

/** Uploads a CV with no candidate named. The backend reads the document's
 *  contact details, matches an existing candidate by email/phone or creates a
 *  new one, then queues the roles/skills parse. Returns the document row in
 *  `ingest_pending` — the candidate is resolved asynchronously by the ingest
 *  job, so the caller re-lists candidates rather than expecting a finished one.
 *  202, like the per-candidate path: the file is stored and queued, not read. */
export async function uploadCandidateDocumentNoCandidate(
  file: File,
): Promise<CandidateDocument> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(CANDIDATES_DOCUMENTS_PATH, {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
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

/** Namespaces this candidate's photo inside the shared signed-URL cache, so a
 *  candidate id and a client id that happen to collide cannot swap images. */
const AVATAR_CACHE_KIND = "candidate-avatar";

/** A presigned URL good for roughly `expires_in` seconds — `null` means the
 *  candidate has no photo (a 404), not a failed request.
 *
 *  Memoised in memory for the URL's own lifetime, keyed on `version`
 *  (`avatar_updated_at`), so re-opening the same panel returns the *same* URL
 *  string and the browser reuses the image it already has instead of
 *  downloading it again. See `signed-url-cache.ts` for why that string has to
 *  be stable and why nothing here is persisted. Callers that have no version
 *  to pass get the old behaviour: a fresh request every time. */
export async function getCandidateAvatar(
  id: string,
  version: string | null = null,
): Promise<AvatarUrl | null> {
  const fetchIt = async (): Promise<AvatarUrl | null> => {
    const res = await fetch(candidateAvatarPath(id), {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (res.status === 404) return null;
    if (!res.ok) throw new ApiError(await readError(res));
    return (await res.json()) as AvatarUrl;
  };
  if (version === null) return fetchIt();
  return cachedSignedUrl(cacheKey(AVATAR_CACHE_KIND, id, version), fetchIt);
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

/** The languages the WhatsApp modal can render the draft in. English is the
 *  canonical source produced by `whatsapp_draft_text`; the others are
 *  translated on demand. The wire values match `WHATSAPP_LANGUAGES` on the
 *  server, so this is the single source of truth for the selector's option
 *  set on both sides. */
export type WhatsappLanguage = "english" | "chinese" | "malay" | "tamil";

/** Translates the current English text into another language. The caller sends
 *  the English source on every non-English switch — never the previously shown
 *  translation — so two non-English languages never translate from each other.
 *  `english` round-trips the source unchanged (the server handles it without
 *  calling the translation service), so the same function covers switching back.
 *
 *  Throws `ApiError` carrying the server's `detail` on a 502 (service down) or
 *  422 (unsupported language); the modal shows that detail and reverts the
 *  selector, so the box never holds a language the selector does not show. */
export async function translateWhatsappDraft(
  id: string,
  sourceText: string,
  targetLanguage: WhatsappLanguage,
): Promise<string> {
  const res = await fetch(candidateWhatsappTranslatePath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ source_text: sourceText, target_language: targetLanguage }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { translation: string };
  return body.translation;
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
  status: "sent" | "unknown" | "failed" | "pending";
  activity_id: string;
  provider_message_id: string | null;
  client_request_id: string;
};

/** A 409/429/422 is not a generic failure — it carries the server's own
 *  sentence, so the modal can say something a recruiter can act on. `kind`
 *  distinguishes the three because each has a different fix: `session` (409,
 *  `session_status` set) means link or wait; `rate_limited` (429) means a
 *  send was refused before dispatch — the popup is still open to them;
 *  `no_number` (422) means the fix is on the candidate record, not in
 *  Settings → WhatsApp, so the modal must not point there.
 *
 *  `risk_not_acknowledged` (409, same status as `session`, told apart by
 *  `reason` in the body rather than `session_status` — the session may be
 *  perfectly `connected`, this refusal is about the recruiter, not the
 *  link) means the fix is one checkbox in Settings → WhatsApp, and *only*
 *  that: the popup is not offered as a way around it, because the refusal
 *  exists to make sure the recruiter has seen the risk notice before
 *  sending, and routing around it defeats the point.
 *
 *  There are two distinct 429s, told apart by `limit` rather than by
 *  guessing from the sentence: `"daily"` is the day's cap, spent until UTC
 *  midnight; `"interval"` is the minimum gap between sends, which clears
 *  itself in seconds. `retry_after_seconds` is always the server's own
 *  number — never recomputed here, since a rounded or drifted value just
 *  produces a second 429 on retry. */
export class WhatsappSendError extends ApiError {
  kind: "session" | "rate_limited" | "no_number" | "risk_not_acknowledged";
  session_status: string | null;
  limit: "daily" | "interval" | null;
  retry_after_seconds: number | null;
  constructor(
    message: string,
    kind: "session" | "rate_limited" | "no_number" | "risk_not_acknowledged",
    session_status: string | null,
    limit: "daily" | "interval" | null = null,
    retry_after_seconds: number | null = null,
  ) {
    super(message);
    this.kind = kind;
    this.session_status = session_status;
    this.limit = limit;
    this.retry_after_seconds = retry_after_seconds;
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
    let limit: "daily" | "interval" | null = null;
    let retry_after_seconds: number | null = null;
    let reason: string | null = null;
    try {
      const body = (await res.json()) as {
        detail?: string;
        session_status?: string;
        limit?: "daily" | "interval";
        retry_after_seconds?: number;
        reason?: string;
      };
      if (body.detail) detail = body.detail;
      if (body.session_status) session_status = body.session_status;
      if (body.limit) limit = body.limit;
      if (typeof body.retry_after_seconds === "number") retry_after_seconds = body.retry_after_seconds;
      if (body.reason) reason = body.reason;
    } catch {
      /* not JSON, or empty */
    }
    // A 409 with `reason: "risk_not_acknowledged"` carries no `session_status`
    // at all — it is not a session state, so it must be told apart by `reason`
    // before falling into the `session` bucket, not folded into it.
    const kind =
      res.status === 429
        ? "rate_limited"
        : res.status === 422
          ? "no_number"
          : res.status === 409 && reason === "risk_not_acknowledged"
            ? "risk_not_acknowledged"
            : "session";
    throw new WhatsappSendError(detail, kind, session_status, limit, retry_after_seconds);
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
  // Removal is the one change with no new `avatar_updated_at` to miss the
  // cache on — the column goes null — so the entry has to be dropped by hand.
  forget(AVATAR_CACHE_KIND, id);
}

// ------------------------------------------------ ownership and sharing
//
// Every one of these lives here rather than in the component that calls it,
// for the reason the module docstring gives: this is the one place that talks
// to the candidates endpoint, and a path built inline in a component is
// invisible to `app/api.contract.test.ts`. That has already shipped two dead
// routes in this repo.
//
// A share grants SIGHT and nothing else, exactly as on the job order side —
// there is no access level here, and adding one would invent a concept the
// server does not have.

export type CandidateShareScope = "user" | "tenant";

export type CandidateShare = {
  id: string;
  scope: CandidateShareScope;
  /** Null exactly for the tenant broadcast, which is shared with everybody
   *  rather than with a particular person. */
  shared_with_user_id: string | null;
  shared_by_user_id: string | null;
  note: string | null;
  created_at: string;
};

export type CandidateShareRequest = {
  scope: CandidateShareScope;
  user_ids: string[];
  /** Trimmed to `null` by the caller: an empty string is a note that says
   *  nothing, and storing one puts a blank line in the notification. */
  note: string | null;
};

/** One person asking to be shown a candidate they cannot see.
 *
 * Deliberately says nothing about the candidate beyond its id — the server's
 * inbox route returns no name, because naming the row here would leak through
 * the inbox what the request route itself refuses to return. The owner already
 * knows who they hold; the id is what the grant needs. */
export type CandidateAccessRequest = {
  id: string;
  candidate_id: string;
  requested_by_user_id: string;
  status: "pending" | "granted" | "declined";
  note: string | null;
  created_at: string;
  resolved_at: string | null;
};

/** Who can currently see this candidate, oldest first — the server's order,
 *  kept, because it reads as the history of how the row travelled. */
export async function listCandidateShares(id: string): Promise<CandidateShare[]> {
  const res = await fetch(candidateSharesPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { items?: CandidateShare[] };
  return body.items ?? [];
}

/** Shows this candidate to named colleagues, or to the whole agency.
 *
 * Returns how many people were newly given sight — re-sharing to somebody who
 * already had it counts for nothing, because they were told the first time.
 * The broadcast is 403 for anyone but the owner, which the dialog mirrors
 * rather than discovers. */
export async function shareCandidate(
  id: string,
  body: CandidateShareRequest,
): Promise<{ newly_shared_with: number }> {
  const res = await fetch(candidateSharesPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as { newly_shared_with: number };
}

/** Takes sight back, and only sight. Activities the recipient logged while
 *  they could see the candidate record what that person did; they are not the
 *  candidate, and the server keeps them. */
export async function deleteCandidateShare(id: string, shareId: string): Promise<void> {
  const res = await fetch(candidateSharePath(id, shareId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** Asks the owner to be shown a candidate. The one candidate route reachable
 *  for a row you cannot see — a second click updates the one pending request
 *  rather than filing a second. */
export async function requestCandidateAccess(
  id: string,
  note: string | null,
): Promise<{ id: string }> {
  const res = await fetch(candidateAccessRequestsPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ note }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as { id: string };
}

/** The inbox: requests waiting on ME, scoped server-side to candidates I own.
 *  Defaults to pending, which is the only status anyone can act on. */
export async function listCandidateAccessRequests(
  status: CandidateAccessRequest["status"] = "pending",
): Promise<CandidateAccessRequest[]> {
  const params = new URLSearchParams({ status });
  const res = await fetch(`${CANDIDATE_ACCESS_REQUESTS_PATH}?${params.toString()}`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { items?: CandidateAccessRequest[] };
  return body.items ?? [];
}

/** Yes. Writes the share and closes the request in one server transaction —
 *  the share IS the grant. */
export async function grantCandidateAccess(id: string, requestId: string): Promise<void> {
  const res = await fetch(candidateAccessRequestGrantPath(id, requestId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** No — and saying it is not optional politeness. A request that never
 *  resolves leaves the asker waiting, and then asking again. */
export async function declineCandidateAccess(id: string, requestId: string): Promise<void> {
  const res = await fetch(candidateAccessRequestDeclinePath(id, requestId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** Takes an unclaimed candidate. A 409 here means a colleague got there
 *  first — the server resolves the race in the database, and its message is
 *  the one worth showing. */
export async function claimCandidate(id: string): Promise<void> {
  const res = await fetch(candidateClaimPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** Hands a candidate to a colleague, or — with `null` — lets go of it back to
 *  the queue. One call for both, because handing over and letting go are the
 *  same act with a different destination. */
export async function assignCandidate(id: string, userId: string | null): Promise<void> {
  const res = await fetch(candidateAssignPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/**
 * Whether this reader may edit this candidate — the server's rule, PUBLISHED,
 * not re-derived.
 *
 * `can_edit` on the payload already is `can_edit_candidate` on the server:
 * the `owner` role, or you are the owner. Nothing here recomputes that from
 * `owner.id === me.id` or a role check — a second copy of "who may edit" is
 * exactly how the two drift apart, and the UI has no better information than
 * the server that answered this request in the first place.
 *
 * Fails CLOSED on a missing value, unlike the version of this function that
 * shipped before `can_edit` existed on the wire. That version's `undefined`
 * branch answered `true` because the field genuinely did not exist yet, and
 * refusing on its absence would have greyed out every Edit button in the app.
 * `can_edit` is on every candidate payload now (2c6051f) — list rows and the
 * single-record read alike — so a row without it is not the server staying
 * silent by design; it is stale or malformed data, and guessing "editable"
 * for a permission flag is the wrong default to guess.
 */
export function canEditCandidate(row: Pick<Candidate, "can_edit">): boolean {
  return row.can_edit === true;
}
