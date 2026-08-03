"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CLIENTS_PAGE_SIZE,
  CLIENTS_PATH,
  clientArchivePath,
  clientAssigneePath,
  clientCollaboratorPath,
  clientCollaboratorsPath,
  clientConfirmPath,
  clientContactPath,
  clientContactsPath,
  clientLogoPath,
  clientMergePath,
  clientPath,
  clientRestorePath,
  clientSuspendPath,
  clientUnmergePath,
  clientUnsuspendPath,
} from "../api";
import { cacheKey, cachedSignedUrl, forget } from "./signed-url-cache";

/**
 * The agency's client list, and the one place that talks to the clients
 * endpoint.
 *
 * Most rows are proposed by the ingestion pipeline rather than typed by a
 * person, so the state transitions a human makes over a proposal — confirm,
 * archive, restore, merge, unmerge — are the centre of this file. A recruiter
 * can also add and edit a client by hand (`createClient`, `updateClient`,
 * `suspendClient` and the contact calls below): a client that has never sent
 * an email is still a client, and `source` records which way a row arrived.
 * There is still no delete — nothing here is destroyed, only archived.
 *
 * Follows the same fetch and typing pattern as `candidates.ts`:
 * `credentials: "include"`, an `Accept: application/json` header, and
 * `URLSearchParams` for the query string.
 */

export type ClientStatus =
  | "unconfirmed"
  | "confirmed"
  | "suspended"
  | "archived"
  | "merged";

// "human" is not included: `client_matching.py` only ever writes
// "email_domain" or "name" (checked at `_resolve` and its callers) — there is
// no code path that records a manual match.
export type MatchedBy = "email_domain" | "name";

export type ClientMention = {
  id: string;
  email_message_id: string | null;
  matched_by: MatchedBy;
  created_at: string;
};

export type Contact = {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  title: string | null;
  is_primary: boolean;
  created_at: string;
};

export type ContactInput = {
  name: string;
  email?: string | null;
  phone?: string | null;
  title?: string | null;
  is_primary?: boolean;
};

export type ClientInput = {
  name: string;
  email_domain?: string | null;
  website?: string | null;
  phone?: string | null;
  address?: string | null;
  fee_percent?: number | null;
  payment_terms_days?: number | null;
  notes?: string | null;
};

export type Collaborator = {
  user_id: string;
  /** Resolved server-side like `Member.name`. Null when the row outlives the
   *  person — nothing here invents a name (§15). */
  name: string | null;
};

export type Client = {
  id: string;
  name: string;
  name_normalized: string;
  email_domain: string | null;
  status: ClientStatus;
  merged_into_client_id: string | null;
  last_seen_at: string | null;
  created_at: string;
  website: string | null;
  phone: string | null;
  address: string | null;
  fee_percent: number | null;
  payment_terms_days: number | null;
  notes: string | null;
  source: "pipeline" | "manual";
  suspended_reason: string | null;
  suspended_at: string | null;
  logo_key: string | null;
  logo_updated_at: string | null;
  /**
   * The recruiter answerable for this account, and their resolved name.
   *
   * Optional rather than required: older cached responses (and any client
   * built from a partial payload) may predate these fields, so a value may be
   * absent rather than merely null. Everything reading them must treat absent
   * and null alike — an assignment control that mistook `undefined` for
   * "nobody" would offer to reassign a client that is already held.
   */
  assigned_user_id?: string | null;
  assignee_name?: string | null;
  /** Colleagues recorded as also covering the account. Grants no access to
   *  the client's job orders — see `ClientCollaborator`. */
  collaborators?: Collaborator[];
  /** Only present on the single-record GET, not on a list row. */
  mentions?: ClientMention[];
  /** Only present on the single-record GET, not on a list row. */
  contacts?: Contact[];
};

export type ClientPage = {
  items: Client[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
};

/** The chips. `null` is "All" (every non-merged row, the review queue plus
 *  everything already confirmed or archived). `"merged"` is reached only by
 *  explicit filter — merged rows are excluded from the default list and the
 *  pointer runs loser -> survivor with no link back, so without this chip a
 *  wrongly merged client could never be found again to unmerge. */
export type Filter = null | ClientStatus;

function listUrl(filter: Filter, offset: number, limit: number): string {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filter) params.set("status", filter);
  return `${CLIENTS_PATH}?${params.toString()}`;
}

function messageFor(status: number): string {
  // A 401 is our session expiring, not the client list failing. Saying "we
  // could not load your clients" for an expired cookie sends someone to look
  // at the wrong thing entirely.
  return status === 401
    ? "Your session has expired. Sign in again to see your clients."
    : "We could not load your clients just now.";
}

export type ListState =
  | { status: "loading" }
  | { status: "ready"; page: ClientPage }
  | { status: "unreadable"; message: string };

const ZERO_COUNTS: Record<string, number> = { all: 0 };

export type Clients = {
  state: ListState;
  filter: Filter;
  offset: number;
  /** What is being asked for — distinct from `state.page.limit`, which is
   *  what the server actually used and clamps. Mirrors `limit` in
   *  `candidates.ts` and `opportunities.ts`: the select the page-size control
   *  renders must show what was asked, because a select whose value disagrees
   *  with its options renders blank. */
  limit: number;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. */
  counts: Record<string, number>;
  setFilter: (filter: Filter) => void;
  setOffset: (offset: number) => void;
  /** Back to the first page for the same reason `setFilter` does: offset 150
   *  of a 50-row page is page four, and of a 150-row page it may be past the
   *  end of the list entirely. Growing the page while standing deep in the
   *  list would land on nothing. */
  setLimit: (limit: number) => void;
  reload: () => void;
};

/** The review queue is the centre of gravity here: the default view is the
 *  unconfirmed proposals a recruiter has not yet judged, not the whole
 *  tenant mixed together. */
export function useClients(): Clients {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>("unconfirmed");
  const [offset, setOffset] = useState(0);
  const [limit, setLimitRaw] = useState(CLIENTS_PAGE_SIZE);
  const [counts, setCounts] = useState<Record<string, number>>(ZERO_COUNTS);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    // Rows already on screen stay on screen while the refetch runs — the same
    // reasoning as `useCandidates`, which this list had drifted away from.
    // Unconditionally dropping to `loading` empties the split, and emptying
    // the split unmounts the detail panel beside it, so a reader who changes
    // rows-per-page watches the client they were reading disappear and come
    // back. It was survivable while every refetch followed a click that
    // changed which rows belonged on screen; a page-size control is the first
    // one where the selected row is meant to still be there afterwards.
    setState((prev) => (prev.status === "ready" ? prev : { status: "loading" }));
    (async () => {
      try {
        const res = await fetch(listUrl(filter, offset, limit), {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({ status: "unreadable", message: messageFor(res.status) });
          return;
        }
        const page = (await res.json()) as ClientPage;
        setState({ status: "ready", page });
        setCounts(page.counts);
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
    return () => controller.abort();
  }, [filter, offset, limit, nonce]);

  const setFilter = useCallback((next: Filter) => {
    setFilterRaw(next);
    setOffset(0);
  }, []);
  // A page-size change is a filter change like any other for the purposes of
  // the offset, and for the same reason `candidates.ts` resets it: standing
  // on offset 150 when the page grows to 150 rows is standing past the end
  // of a 200-row list.
  const setLimit = useCallback((next: number) => {
    setLimitRaw(next);
    setOffset(0);
  }, []);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { state, filter, offset, limit, counts, setFilter, setOffset, setLimit, reload };
}

/** The one server field name a 422 is checked against when picking which
 *  entry of a multi-error `detail` list to surface inline — the free-provider
 *  domain rule in `app/api/clients.py`. Shared with `client-form.tsx`, which
 *  is the only place that renders it against a field, so there is exactly one
 *  spelling of it. */
export const DOMAIN_FIELD = "email_domain";

/** Turns a non-2xx response into the server's own message where it gave one,
 *  rather than a generic failure. A 409 on merge or unmerge names the
 *  conflicting client — the message is the only thing telling the recruiter
 *  what to do next. */
async function readError(res: Response): Promise<string> {
  return (await readProblem(res)).message;
}

/** A refusal, and which request field it was about when the server said so.
 *  `field` is `null` for everything that is not about one field — a 409, a
 *  400, an expired session. */
type Problem = { message: string; field: string | null };

/** FastAPI's `HTTPException` puts a string in `detail`; a body that fails
 *  validation puts a *list* of `{loc, msg}` entries there instead. The
 *  free-provider domain rule is the second kind — a `field_validator` in
 *  `app/api/clients.py` — so without this branch its sentence would reach the
 *  recruiter as `[object Object]`. Nothing here rewrites the server's words;
 *  it only finds them.
 *
 *  Pydantic prefixes a `ValueError`'s text with "Value error, ". That prefix
 *  is the library's, not the message's, and it is dropped: the sentence the
 *  server author wrote is the sentence the recruiter should read. */
const PYDANTIC_VALUE_ERROR_PREFIX = /^Value error,\s*/;

type ValidationEntry = { loc?: unknown[]; msg?: string };

/** `loc` is ["body", "<field>"] for a body field. The last element is the
 *  field; anything shorter is not about a field we render. */
function fieldOfEntry(entry: ValidationEntry): string | null {
  const loc = Array.isArray(entry.loc) ? entry.loc : [];
  const last = loc.length > 1 ? loc[loc.length - 1] : null;
  return typeof last === "string" ? last : null;
}

/** A 422 `detail` is a *list* — pydantic reports every failing field, not
 *  just one. Taking `entries[0]` unconditionally meant a domain error placed
 *  second in the list never reached `DOMAIN_FIELD`'s inline hint, because
 *  whatever validator pydantic ran first won regardless of which field a
 *  form actually renders. `DOMAIN_FIELD` is looked for by name instead, and
 *  any other messages present are not dropped — they ride along in the
 *  banner text so a second, unrendered field error is still visible to the
 *  recruiter rather than silently lost. */
function fromValidationEntries(entries: ValidationEntry[]): Problem | null {
  const withMsg = entries.filter((e): e is ValidationEntry & { msg: string } => typeof e.msg === "string");
  if (withMsg.length === 0) return null;

  const primary = withMsg.find((e) => fieldOfEntry(e) === DOMAIN_FIELD) ?? withMsg[0];
  const clean = (msg: string) => msg.replace(PYDANTIC_VALUE_ERROR_PREFIX, "");
  const rest = withMsg.filter((e) => e !== primary).map((e) => clean(e.msg));

  return {
    message: [clean(primary.msg), ...rest].join(" "),
    field: fieldOfEntry(primary),
  };
}

async function readProblem(res: Response): Promise<Problem> {
  if (res.status === 401) {
    return {
      message: "Your session has expired. Sign in again, then try that once more.",
      field: null,
    };
  }
  try {
    const body = (await res.json()) as { detail?: string | ValidationEntry[] };
    if (typeof body.detail === "string" && body.detail) {
      return { message: body.detail, field: null };
    }
    if (Array.isArray(body.detail)) {
      const problem = fromValidationEntries(body.detail);
      if (problem) return problem;
    }
  } catch {
    /* not JSON, or empty */
  }
  return { message: "We could not save that just now. Nothing has changed.", field: null };
}

export class ApiError extends Error {}

/** A refusal the server tied to one request field, so a form can render it
 *  beside that input instead of as a page-level banner. Still an `ApiError`,
 *  so every existing `instanceof ApiError` handler keeps working and keeps
 *  showing the server's own sentence. */
export class FieldError extends ApiError {
  constructor(
    message: string,
    readonly field: string,
  ) {
    super(message);
  }
}

/** The one place a non-2xx becomes something to throw. */
async function apiError(res: Response): Promise<ApiError> {
  const problem = await readProblem(res);
  return problem.field ? new FieldError(problem.message, problem.field) : new ApiError(problem.message);
}

export async function getClient(id: string): Promise<Client> {
  const res = await fetch(clientPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Client;
}

export async function confirmClient(id: string): Promise<void> {
  const res = await fetch(clientConfirmPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function archiveClient(id: string): Promise<void> {
  const res = await fetch(clientArchivePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function restoreClient(id: string): Promise<void> {
  const res = await fetch(clientRestorePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function mergeClient(id: string, targetId: string): Promise<void> {
  const res = await fetch(clientMergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ target_id: targetId }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function unmergeClient(id: string): Promise<void> {
  const res = await fetch(clientUnmergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** Adds a client by hand, at `confirmed` — the one row type in this file a
 *  person creates rather than the pipeline proposing. A 409 here names the
 *  client that already holds the domain (see `_domain_conflict` in
 *  `app/api/clients.py`); `readError` is what carries that string through. */
export async function createClient(body: ClientInput): Promise<Client> {
  const res = await fetch(CLIENTS_PATH, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as Client;
}

/** Edits the facts a recruiter owns — never `status`, which has its own
 *  endpoint per transition. `body` is sent as given, a `Partial`, so a field
 *  the recruiter never touched is never cleared by an undefined creeping in. */
export async function updateClient(id: string, body: Partial<ClientInput>): Promise<Client> {
  const res = await fetch(clientPath(id), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as Client;
}

/** Puts a live client on hold. `reason` is optional — nothing invents one
 *  when it is absent (§15). A 409 here carries the reason someone already
 *  typed on a prior suspension attempt in flight elsewhere. */
export async function suspendClient(id: string, reason?: string): Promise<void> {
  const res = await fetch(clientSuspendPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function unsuspendClient(id: string): Promise<void> {
  const res = await fetch(clientUnsuspendPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function createContact(clientId: string, body: ContactInput): Promise<Contact> {
  const res = await fetch(clientContactsPath(clientId), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as Contact;
}

export async function updateContact(
  clientId: string,
  contactId: string,
  body: Partial<ContactInput>,
): Promise<Contact> {
  const res = await fetch(clientContactPath(clientId, contactId), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
  return (await res.json()) as Contact;
}

export async function deleteContact(clientId: string, contactId: string): Promise<void> {
  const res = await fetch(clientContactPath(clientId, contactId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export type LogoUrl = { url: string; expires_in: number };

/** Namespaces this client's logo inside the shared signed-URL cache, so a
 *  client id and a candidate id that happen to collide cannot swap images. */
const LOGO_CACHE_KIND = "client-logo";

/** A presigned URL good for roughly `expires_in` seconds — `null` means the
 *  client has no logo (a 404), not a failed request.
 *
 *  Memoised in memory for the URL's own lifetime, keyed on `version`
 *  (`logo_updated_at`) — see `getCandidateAvatar`, which this mirrors. It
 *  matters more here: the sourcing list renders one `ClientLogo` per card
 *  (`job-orders-sourcing.tsx`), several of them routinely the same client, so
 *  without this each card signs and downloads the same logo for itself. */
export async function getClientLogo(
  id: string,
  version: string | null = null,
): Promise<LogoUrl | null> {
  const fetchIt = async (): Promise<LogoUrl | null> => {
    const res = await fetch(clientLogoPath(id), {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (res.status === 404) return null;
    if (!res.ok) throw new ApiError(await readError(res));
    return (await res.json()) as LogoUrl;
  };
  if (version === null) return fetchIt();
  return cachedSignedUrl(cacheKey(LOGO_CACHE_KIND, id, version), fetchIt);
}

export async function uploadClientLogo(
  id: string,
  file: File,
): Promise<{ logo_key: string; logo_updated_at: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(clientLogoPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as { logo_key: string; logo_updated_at: string };
}

/** What the server reports after a reassignment.
 *
 *  `opportunities_moved` is the response key; the *request* field asking for
 *  the move is `move_open_opportunities`. The two names differ, both are fixed
 *  by the shipped API, and neither is tidied here. */
export type AssigneeResult = {
  client_id: string;
  assigned_user_id: string | null;
  opportunities_moved: number;
};

/**
 * Hands the account to a recruiter, and its job orders with it.
 *
 * `moveOpportunities` maps to `move_open_opportunities`. The word "open" is
 * the API's and stays inside this function: no lifecycle state exists on
 * `Opportunity` to make it true, so nothing shown to a recruiter may say it.
 * What actually moves is every job order for this client currently held by the
 * outgoing recruiter.
 *
 * A 403 here is the server refusing a caller who is neither the owner nor the
 * current assignee. The interface hides the control in exactly those cases, so
 * reaching this branch means the client changed hands under the reader.
 */
export async function setClientAssignee(
  id: string,
  userId: string | null,
  moveOpportunities: boolean,
): Promise<AssigneeResult> {
  const res = await fetch(clientAssigneePath(id), {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ user_id: userId, move_open_opportunities: moveOpportunities }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as AssigneeResult;
}

/** Records that a colleague also knows this account. Idempotent server-side
 *  (`ON CONFLICT DO NOTHING`), so naming the same person twice is a success,
 *  not an error, and nothing here special-cases it. */
export async function addCollaborator(clientId: string, userId: string): Promise<void> {
  const res = await fetch(clientCollaboratorsPath(clientId), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** Removing cover that is already gone is a no-op, for the same reason. */
export async function removeCollaborator(clientId: string, userId: string): Promise<void> {
  const res = await fetch(clientCollaboratorPath(clientId, userId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function deleteClientLogo(id: string): Promise<void> {
  const res = await fetch(clientLogoPath(id), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  // Removal is the one change with no new `logo_updated_at` to miss the cache
  // on — the column goes null — so the entry has to be dropped by hand.
  forget(LOGO_CACHE_KIND, id);
}
