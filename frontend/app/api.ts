// Where the API lives, in one place.
//
// The site is a static export, so this is baked in at build time. Empty means
// same-origin — in production one service serves both the site and /api — and
// NEXT_PUBLIC_API_BASE overrides it for local development against a separate
// backend.
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/** Starts the Microsoft OAuth flow; the API redirects on to Microsoft. */
export const SIGN_IN_PATH = `${API_BASE}/api/auth/microsoft/login`;

/**
 * The same flow, but forcing Microsoft's account picker.
 *
 * Needed because Microsoft, asked plainly, reuses whatever account already has
 * a browser SSO session and never shows a chooser. Clearing our session cookie
 * does not touch that, so without this a returning user is silently signed
 * back into the one account they have ever used and has no way to reach a
 * second one — the personal account they tried first, when the agency mailbox
 * is on their work account.
 *
 * Used for every deliberate *choice* of account (switching, and signing in
 * after a sign-out), never for a plain first visit, where the picker is one
 * pointless click.
 */
export const SWITCH_ACCOUNT_PATH = `${SIGN_IN_PATH}?prompt=select_account`;

/* No EARLY_ACCESS_PATH: the early-access form was removed when sign-in went
   live, and the site now has no consumer for it. The backend route and the
   signups table still exist and still hold the submissions already collected. */

/**
 * Asks Microsoft, separately from sign-in, for permission to read the mailbox.
 *
 * Separate because the two consents face different bars: many tenants let a
 * user consent to identity scopes but not to mailbox ones. Bundled, that made
 * signing in itself need an administrator; split, the wall arrives only for
 * the person who asked for their mail to be read.
 */
export const CONNECT_MAILBOX_PATH = `${API_BASE}/api/auth/microsoft/connect-mailbox`;

/**
 * What is in the connected inbox, before any of it is imported.
 *
 * Needs the grant, so it can only be asked after consent — which is why the
 * choice comes second rather than first. Costs a few Graph calls and reads
 * nothing into our own storage.
 */
export const MAILBOX_PREVIEW_PATH = `${API_BASE}/api/mailbox/preview`;

/**
 * The extracted job orders, newest first — the table that replaces the
 * spreadsheet. Read-only, and bounded server-side, so the dashboard renders
 * whatever it is given without paging.
 */
export const OPPORTUNITIES_PATH = `${API_BASE}/api/opportunities`;

/** How many job orders one page of the list asks for. Same env-override and
 *  NaN-guard reasoning as `CANDIDATES_PAGE_SIZE` below: search and sort now
 *  run on the server, so a smaller page is a deployment decision, not a
 *  correctness one. */
export const OPPORTUNITIES_PAGE_SIZE =
  Number(process.env.NEXT_PUBLIC_OPPORTUNITIES_PAGE_SIZE) || 10;

/** What the page-size control offers. The configured default is folded in and
 *  the result deduped, so an operator who sets the env var to something not on
 *  this list still gets a control whose value is one of its own options — a
 *  select whose value matches nothing renders blank and looks broken.
 *
 *  The server clamps `limit` to `OPPORTUNITIES_PAGE_LIMIT` (200) rather than
 *  rejecting it, so nothing here can ask for a page it will not get. */
export const OPPORTUNITIES_PAGE_SIZES: readonly number[] = [
  ...new Set([10, 20, 30, OPPORTUNITIES_PAGE_SIZE]),
].sort((a, b) => a - b);

/**
 * Marks one job order as checked by a human, or un-marks it.
 *
 * A function rather than a constant because the id is in the path. Encoded, so
 * an id that is not the uuid we expect cannot walk out of its own segment.
 */
export function opportunityReviewPath(id: string): string {
  return `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/review`;
}

/**
 * The shortlist for one job order: POST starts a run, GET reads back the most
 * recent one with its matches.
 *
 * A function rather than a constant for the reason `opportunityReviewPath` is
 * one — the id is in the path, and encoded so an id that is not the uuid we
 * expect cannot walk out of its own segment.
 */
export function opportunitySourcingPath(id: string): string {
  return `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/sourcing`;
}

/** One job order, for GET — the whole row, in the shape one row of the list
 *  comes back in. Read after every write, because each write route answers
 *  with only the fields it changed. Same id-in-path, same encoding, as the
 *  other single-row paths on this page. */
export function opportunityPath(id: string): string {
  return `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}`;
}

/**
 * The two halves of the placement form, which are two routes and not one.
 *
 * They are separate on the server because they are separate decisions, each
 * with its own audit columns: `placement_type` decides which Work Permit rules
 * the eligibility check runs, and the sex requirement is a lawful judgement
 * about the job itself. POST, not PATCH — there is no combined write, and the
 * form sends only the half that changed.
 */
export function opportunityPlacementTypePath(id: string): string {
  return `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/placement-type`;
}

export function opportunityOccupationalRequirementPath(id: string): string {
  return `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/occupational-requirement`;
}

/**
 * Taking an unassigned job order for yourself. POST-only. A 409 means someone
 * else got there first — an ordinary race between two recruiters, not an
 * error — and a 404 means it is not visible to you, which deliberately does
 * not say whether the row exists.
 */
export const opportunityClaimPath = (id: string) =>
  `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/claim`;

/** Handing a job order to someone else, or — with `user_id: null` — back to
 *  the unassigned queue. 403 when you may see it but not edit it. */
export const opportunityAssignPath = (id: string) =>
  `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/assign`;

/** Linking a job order to the client it came from, or — with `client_id:
 *  null` — unlinking it. 403 when you may see it but not edit it, and 422 when
 *  the client belongs to another agency. */
export const opportunityClientPath = (id: string) =>
  `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/client`;

/** Who else can see this job order: GET lists, POST shares it with one
 *  colleague. */
export const opportunitySharesPath = (id: string) =>
  `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/shares`;

/** One share, for DELETE. Both ids are encoded for the reason every id-in-path
 *  function on this page encodes its id. */
export const opportunitySharePath = (id: string, shareId: string) =>
  `${OPPORTUNITIES_PATH}/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}`;

/**
 * Whether one candidate meets the placement requirements for one job order —
 * MOM Work Permit eligibility, never a hiring verdict. Both ids are path
 * segments and both are encoded for the reason every id-in-path function on
 * this page encodes its id.
 */
export function opportunityCandidateEligibilityPath(
  opportunityId: string,
  candidateId: string,
): string {
  return `${OPPORTUNITIES_PATH}/${encodeURIComponent(opportunityId)}/candidates/${encodeURIComponent(candidateId)}/eligibility`;
}

/**
 * How often the panel asks again while a run is still working.
 *
 * Scoring an agency's whole candidate database and then calling a model takes
 * as long as it takes, so this is a poll rather than an event: nothing on the
 * event stream is emitted for a shortlist. Env-overridable with the same
 * `|| 4000` guard as the page sizes above — an unset variable and a typo both
 * parse to `NaN`, and `setInterval(NaN)` fires as fast as the browser will let
 * it.
 */
export const SOURCING_POLL_MS = Number(process.env.NEXT_PUBLIC_SOURCING_POLL_MS) || 4000;

/**
 * Records that a candidate was put in front of a client — the one durable fact
 * the shortlist exists to produce, and what makes the "not already submitted"
 * exclusion mean anything on the next run.
 */
export function candidateSubmissionsPath(id: string): string {
  return `${candidatePath(id)}/submissions`;
}

/**
 * What ingestion has actually been doing — the last few events, each with the
 * outcome. Separate from the counts on `/auth/me` because a count says how
 * much arrived and this says whether anything is going wrong.
 */
export const MAILBOX_ACTIVITY_PATH = `${API_BASE}/api/mailbox/activity`;

/** POST the chosen period. The only thing that starts ingestion. */
export const MAILBOX_INGEST_PATH = `${API_BASE}/api/mailbox/ingest`;

/** Who is signed in: 200 with user/tenant/mailbox, or 401 when nobody is. */
export const ME_PATH = `${API_BASE}/api/auth/me`;

/**
 * The open stream that tells the dashboard something changed.
 *
 * Mail arrives at the backend as a Graph notification, so the browser is never
 * a party to it and can only find out by being told. This is how it is told.
 * Server-sent events rather than a poll, because a poll is a question asked
 * every few seconds to which the answer is almost always "nothing" — paid for
 * by every open dashboard, forever, to be a few seconds late anyway.
 *
 * Same-origin and cookie-authenticated, which is the reason SSE is available at
 * all here: `EventSource` cannot set headers, so a bearer scheme would have
 * ruled it out and left a WebSocket or a poll as the only options.
 */
export const EVENTS_PATH = `${API_BASE}/api/events`;

/** Clears the session cookie. POST only. */
export const LOGOUT_PATH = `${API_BASE}/api/auth/logout`;

/** The landing page — where a signed-out visitor is sent from a guarded route. */
export const LANDING_PATH = "/";

/**
 * Marks the landing page as "you just signed out", so the sign-in button there
 * offers the account picker instead of silently resuming the account that was
 * just left. Someone who signs out and immediately signs in again is, far more
 * often than not, trying to get into a different account.
 */
export const CHOOSE_ACCOUNT_PARAM = "choose-account";
export const LANDING_AFTER_SIGN_OUT = `${LANDING_PATH}?${CHOOSE_ACCOUNT_PARAM}=1`;

/** The signed-in shell. A site route, not an API one — no API_BASE prefix. */
export const DASHBOARD_PATH = "/dashboard";

/** Where the account menu points. A site route, so no API_BASE prefix. */
export const SETTINGS_PATH = "/settings";

/** The candidate list. A site route, so no API_BASE prefix. */
export const CANDIDATES_DASHBOARD_PATH = "/dashboard/candidates";

/**
 * The settings sub-routes. Site routes, so no API_BASE prefix.
 *
 * `/settings` itself keeps the inbox setting rather than redirecting to
 * `/settings/inbox`: this is a static export, so a redirect would have to
 * render something first and flash, and existing links to `/settings` still
 * land where they always did.
 */
export const SETTINGS_GLOSSARY_PATH = "/settings/glossary";
export const SETTINGS_NOTIFICATIONS_PATH = "/settings/notifications";
export const SETTINGS_ACCOUNT_PATH = "/settings/account";
export const SETTINGS_WHATSAPP_PATH = "/settings/whatsapp";
export const SETTINGS_CLIENT_DISCOVERY_PATH = "/settings/client-discovery";

/**
 * Client auto-discovery: a header-only scan of the signed-in recruiter's own
 * mailbox that backfills contacts onto existing clients and offers the ranked
 * new domains for one-click creation.
 *
 * GET reads the latest run back (null when none has been started); the two
 * POSTs start a scan and create the selected domains as confirmed clients.
 */
export const CLIENT_DISCOVERY_PATH = `${API_BASE}/api/client-discovery`;
export const CLIENT_DISCOVERY_SCAN_PATH = `${CLIENT_DISCOVERY_PATH}/scan`;
export const CLIENT_DISCOVERY_CREATE_PATH = `${CLIENT_DISCOVERY_PATH}/clients`;

/**
 * How often the panel asks again while a scan is still working. A poll, not
 * an event, for the reason `SOURCING_POLL_MS` is one — and with the same
 * `|| 4000` guard: an unset variable parses to `NaN`, and `setInterval(NaN)`
 * fires as fast as the browser allows.
 */
export const CLIENT_DISCOVERY_POLL_MS =
  Number(process.env.NEXT_PUBLIC_CLIENT_DISCOVERY_POLL_MS) || 4000;

/**
 * The recruiter's own WhatsApp link — the "WA gateway" (Baileys), a separate
 * concern from the `whatsapp`/`WHATSAPP_*` Meta Cloud API integration that
 * sends notifications to recruiters. The two are deliberately namespaced apart
 * (`wa` vs `whatsapp`) so nothing here is ever confused with that channel.
 *
 * `POST /session` starts or resumes pairing; `GET /session` reads the current
 * state; `POST /session/disconnect` tears the link down. All three return the
 * DB-backed status vocabulary: `pairing | connected | reconnecting |
 * disconnected | logged_out`, plus the API-only `gateway_unreachable`.
 */
export const WA_SESSION_PATH = `${API_BASE}/api/wa/session`;
export const WA_SESSION_DISCONNECT_PATH = `${WA_SESSION_PATH}/disconnect`;

/** Records this recruiter's acknowledgement of the unofficial-connection risk
 *  notice returned alongside `GET /api/wa/session`. Body carries the
 *  `notice_version` the recruiter was shown; 422 if the server's current
 *  version has since moved on. */
export const WA_CONSENT_PATH = `${API_BASE}/api/wa/consent`;

/**
 * Everything the notifications screen needs in one read: which channels are
 * configured, which destinations exist with the events each is subscribed to,
 * and the catalogue of events. One endpoint so the screen cannot show a
 * destination and its subscriptions in disagreement.
 */
export const NOTIFICATIONS_SETTINGS_PATH = `${API_BASE}/api/notifications/settings`;

/**
 * Replaces one destination's subscriptions with exactly the set sent.
 *
 * Replace, not merge — the screen is the source of truth for which boxes are
 * ticked, and a merge would make unticking impossible. Callers must always
 * send the full set.
 */
export const NOTIFICATIONS_SUBSCRIPTIONS_PATH = `${API_BASE}/api/notifications/subscriptions`;

/** Mints a single-use `t.me` deep link. The response carries its own expiry. */
export const TELEGRAM_LINK_PATH = `${API_BASE}/api/notifications/destinations/telegram/link`;

/** One destination, for DELETE. Encoded, so an id that is not the uuid we
 *  expect cannot walk out of its own path segment. */
export function notificationDestinationPath(id: string): string {
  return `${API_BASE}/api/notifications/destinations/${encodeURIComponent(id)}`;
}

/**
 * The current "how far back" setting, and the periods that would extend it.
 *
 * Only periods *earlier* than the current one come back. Moving the date later
 * removes nothing already imported, so a shorter option would read as a delete
 * and behave as a no-op — the server decides that rather than the page, and
 * refuses one anyway.
 */
export const MAILBOX_SETTINGS_PATH = `${API_BASE}/api/mailbox/settings`;

/** POST a period to reach further back. Re-runs the historical walk. */
export const MAILBOX_LOOKBACK_PATH = `${MAILBOX_SETTINGS_PATH}/lookback`;

/**
 * The agency's shorthand glossary, and the attributes a code may refer to.
 *
 * Clients write `C/F` and `o/o` in job orders. The decoding is deterministic
 * and comes from this list, so the list is something the agency owns and can
 * correct — a wrong meaning here silently mis-reads every email that uses the
 * code. GET returns both the codes and the vocabulary of attributes; the page
 * never carries its own copy of that vocabulary.
 */
export const GLOSSARY_PATH = `${API_BASE}/api/glossary`;

/** One glossary entry, for PATCH and DELETE. Encoded, so an id that is not the
 *  uuid we expect cannot walk out of its own path segment. */
export function glossaryEntryPath(id: string): string {
  return `${GLOSSARY_PATH}/${encodeURIComponent(id)}`;
}

/**
 * The agency's candidate list — the people it places, as opposed to the
 * vacancies above. Nothing here is AI-derived: every value was typed by a
 * person or came from a spreadsheet a person uploaded.
 */
export const CANDIDATES_PATH = `${API_BASE}/api/candidates`;

/**
 * How many candidates one page of the list asks for.
 *
 * Env-overridable for the same reason as `API_BASE`: an agency with a wall
 * display wants more rows per page than one working on a laptop, and that is a
 * deployment decision, not a code change. Static export, so it is baked in at
 * build time.
 *
 * `|| 10` rather than `??`: an unset variable parses to `NaN`, as does a typo
 * like `ten`, and `limit=NaN` is a 422 from the API — a bad value must fall
 * back to a working page, not break the screen.
 */
export const CANDIDATES_PAGE_SIZE = Number(process.env.NEXT_PUBLIC_CANDIDATES_PAGE_SIZE) || 10;

/** One candidate, for GET/PATCH/DELETE. Encoded, so an id that is not the
 *  uuid we expect cannot walk out of its own path segment. */
export function candidatePath(id: string): string {
  return `${CANDIDATES_PATH}/${encodeURIComponent(id)}`;
}

export function candidateArchivePath(id: string): string {
  return `${candidatePath(id)}/archive`;
}

/** Who else can see this candidate: GET lists, POST shares it. Mirrors
 *  `opportunitySharesPath` — one sharing idiom in this codebase, not two, and
 *  the server module says the same about itself. */
export function candidateSharesPath(id: string): string {
  return `${candidatePath(id)}/shares`;
}

/** One share, for DELETE. Both ids encoded for the reason `candidatePath`
 *  encodes one. */
export function candidateSharePath(id: string, shareId: string): string {
  return `${candidateSharesPath(id)}/${encodeURIComponent(shareId)}`;
}

/** Asking to be shown a candidate you cannot see — the one candidate route
 *  that is deliberately reachable for an invisible row. */
export function candidateAccessRequestsPath(id: string): string {
  return `${candidatePath(id)}/access-requests`;
}

/**
 * The inbox: requests waiting on ME.
 *
 * A literal sibling of `/api/candidates/{id}`, not nested under a candidate,
 * and the server declares it first for exactly that reason — otherwise
 * `access-requests` is matched as a candidate id. Nothing here can encode it,
 * because it is not an id.
 */
export const CANDIDATE_ACCESS_REQUESTS_PATH = `${CANDIDATES_PATH}/access-requests`;

/** Answering one request, yes or no. Two paths rather than one with a body,
 *  because that is what the server serves. */
export function candidateAccessRequestGrantPath(id: string, requestId: string): string {
  return `${candidateAccessRequestsPath(id)}/${encodeURIComponent(requestId)}/grant`;
}

export function candidateAccessRequestDeclinePath(id: string, requestId: string): string {
  return `${candidateAccessRequestsPath(id)}/${encodeURIComponent(requestId)}/decline`;
}

/** Taking an unowned candidate out of the queue. */
export function candidateClaimPath(id: string): string {
  return `${candidatePath(id)}/claim`;
}

/** Handing a candidate to a colleague — or, with a null user, letting go of
 *  it back to the queue. One route for both, as the server has it. */
export function candidateAssignPath(id: string): string {
  return `${candidatePath(id)}/assign`;
}

export function candidateRestorePath(id: string): string {
  return `${candidatePath(id)}/restore`;
}

export function candidateMergePath(id: string): string {
  return `${candidatePath(id)}/merge`;
}

export function candidateUnmergePath(id: string): string {
  return `${candidatePath(id)}/unmerge`;
}

/** The candidate's photo. POST (multipart upload) and DELETE both live here;
 *  GET returns a short-lived presigned URL, never the file itself. */
export function candidateAvatarPath(id: string): string {
  return `${candidatePath(id)}/avatar`;
}

/** The pre-filled WhatsApp message for this candidate, plus the number to
 *  send it to. GET only — nothing is sent until the recruiter presses Open
 *  WhatsApp in their own browser tab. */
export function candidateWhatsappDraftPath(id: string): string {
  return `${candidatePath(id)}/whatsapp-draft`;
}

/** The candidate's activity log — now "WhatsApp opened", "sent" and "failed"
 *  entries, each recorded after the corresponding thing actually happened,
 *  never before. */
export function candidateActivitiesPath(id: string): string {
  return `${candidatePath(id)}/activities`;
}

/** Sends the draft message via the recruiter's own linked WhatsApp session
 *  (see `WA_SESSION_PATH`). POST only; flat, matching the shipped sibling
 *  `whatsapp-draft` above rather than a nested `/whatsapp/send`. A 409 means
 *  the session is not `connected` right now, a 429 means the daily send cap
 *  is hit, and a 422 means the candidate has no number — the caller falls
 *  back to the `Open WhatsApp` popup for the first two rather than
 *  dead-ending. */
export function candidateWhatsappSendPath(id: string): string {
  return `${candidatePath(id)}/whatsapp-send`;
}

/** The roles a candidate held. Nested under the candidate because a role has
 *  no meaning apart from one — an id from another agency is a 404 before any
 *  role is reached. POST creates; the collection itself is never fetched
 *  directly, since `GET /candidates/{id}` already embeds it. */
export function candidateRolesPath(id: string): string {
  return `${candidatePath(id)}/roles`;
}

/** One role, for PATCH and DELETE. Both ids are encoded for the same reason
 *  `candidatePath` encodes one: neither is guaranteed to be a bare UUID. */
export function candidateRolePath(id: string, roleId: string): string {
  return `${candidateRolesPath(id)}/${encodeURIComponent(roleId)}`;
}

/** A role a person has vouched for, or thrown out. POST-only, and idempotent
 *  on the server, so a double-click is not an error. Both ids are encoded for
 *  the reason `candidateRolePath` encodes them. */
export function candidateRoleConfirmPath(id: string, roleId: string): string {
  return `${candidateRolePath(id, roleId)}/confirm`;
}

export function candidateRoleRejectPath(id: string, roleId: string): string {
  return `${candidateRolePath(id, roleId)}/reject`;
}

/** The CVs uploaded against a candidate. POST (multipart) lives here; the
 *  collection is never fetched directly, since `GET /candidates/{id}` already
 *  embeds it beside the roles it produced. */
export function candidateDocumentsPath(id: string): string {
  return `${candidatePath(id)}/documents`;
}

/** One uploaded CV, for DELETE. */
export function candidateDocumentPath(id: string, documentId: string): string {
  return `${candidateDocumentsPath(id)}/${encodeURIComponent(documentId)}`;
}

/** A short-lived presigned URL for the original file — the bytes the recruiter
 *  uploaded, not the text we extracted from them. Fetched at the moment it is
 *  needed and never stored, exactly as the avatar's URL is. */
export function candidateDocumentDownloadPath(id: string, documentId: string): string {
  return `${candidateDocumentPath(id, documentId)}/download`;
}

/** The spreadsheet imports an agency has run. POST (multipart) uploads one and
 *  GET lists the recent ones; both live here. */
export const CANDIDATE_IMPORTS_PATH = `${CANDIDATES_PATH}/imports`;

/**
 * How many recent imports the list asks for.
 *
 * The endpoint has no default limit of its own — omit this and an agency three
 * years into using us downloads every import it has ever run to draw a table
 * showing the last few. Env-overridable for the same reason `CANDIDATES_PAGE_SIZE`
 * is, and with the same `|| 8` guard: an unset variable and a typo both parse
 * to `NaN`, and `limit=NaN` is a 422 rather than a working table.
 */
export const CANDIDATE_IMPORTS_LIMIT = Number(process.env.NEXT_PUBLIC_CANDIDATE_IMPORTS_LIMIT) || 8;

/** The workbook carrying the exact headers the parser reads. A plain download,
 *  not JSON — linked directly rather than fetched. */
export const CANDIDATE_IMPORT_TEMPLATE_PATH = `${CANDIDATE_IMPORTS_PATH}/template`;

/** One import. Encoded for the reason `candidatePath` encodes an id. */
export function candidateImportPath(importId: string): string {
  return `${CANDIDATE_IMPORTS_PATH}/${encodeURIComponent(importId)}`;
}

/** A short-lived presigned URL for the row-by-row error report. Signed per
 *  request and never stored: the report names real candidates, so a URL kept
 *  anywhere would outlive the permission it was granted under. */
export function candidateImportErrorsPath(importId: string): string {
  return `${candidateImportPath(importId)}/errors`;
}

/** Walking one import back. POST-only, and refused while the run is still
 *  parsing. */
export function candidateImportUndoPath(importId: string): string {
  return `${candidateImportPath(importId)}/undo`;
}

/** The client list. A site route, so no API_BASE prefix. */
export const CLIENTS_DASHBOARD_PATH = "/dashboard/clients";

/**
 * The agency's client list — companies the pipeline has proposed from
 * job-order emails. Unlike candidates, there is no create form here: the
 * matcher proposes rows and a human only confirms, merges, archives or
 * unmerges them.
 */
export const CLIENTS_PATH = `${API_BASE}/api/clients`;

/** One client, for GET. Encoded, so an id that is not the uuid we expect
 *  cannot walk out of its own path segment. */
export function clientPath(id: string): string {
  return `${CLIENTS_PATH}/${encodeURIComponent(id)}`;
}

export function clientConfirmPath(id: string): string {
  return `${clientPath(id)}/confirm`;
}

export function clientArchivePath(id: string): string {
  return `${clientPath(id)}/archive`;
}

export function clientRestorePath(id: string): string {
  return `${clientPath(id)}/restore`;
}

export function clientMergePath(id: string): string {
  return `${clientPath(id)}/merge`;
}

export function clientUnmergePath(id: string): string {
  return `${clientPath(id)}/unmerge`;
}

/** Puts a live client on hold, or lifts that hold. Distinct from
 *  archive/restore — see `app/api/clients.py::suspend_client`. */
export function clientSuspendPath(id: string): string {
  return `${clientPath(id)}/suspend`;
}

export function clientUnsuspendPath(id: string): string {
  return `${clientPath(id)}/unsuspend`;
}

/** A client's contacts. POST creates; the collection itself is never fetched
 *  directly, since `GET /clients/{id}` already embeds it. */
export function clientContactsPath(id: string): string {
  return `${clientPath(id)}/contacts`;
}

/** One contact, for PATCH and DELETE. Both ids are encoded for the same
 *  reason `clientPath` encodes one. */
export function clientContactPath(id: string, contactId: string): string {
  return `${clientContactsPath(id)}/${encodeURIComponent(contactId)}`;
}

/** The client's logo: POST to upload, GET for a presigned URL, DELETE to
 *  remove. Mirrors `candidateAvatarPath` — see `clients_logo.py`. */
export function clientLogoPath(id: string): string {
  return `${clientPath(id)}/logo`;
}

/** Which recruiter looks after this client. PUT only — there is no GET; the
 *  current assignee rides on the client record itself. */
export function clientAssigneePath(id: string): string {
  return `${clientPath(id)}/assignee`;
}

/** Who else knows this account. POST records a colleague; it grants nothing
 *  — see `ClientCollaborator` in `app/models/client.py`. */
export function clientCollaboratorsPath(id: string): string {
  return `${clientPath(id)}/collaborators`;
}

/** One collaborator, for DELETE. Both ids encoded, as `clientContactPath`. */
export function clientCollaboratorPath(id: string, userId: string): string {
  return `${clientCollaboratorsPath(id)}/${encodeURIComponent(userId)}`;
}

/**
 * Everyone who works at this agency — who a job order can be assigned to, and
 * who it can be shared with.
 *
 * Names are resolved server-side (preferred name, then display name, then the
 * email's local part) so the browser never re-derives one; a whole list rather
 * than a search endpoint, because an agency here is 3–50 recruiters.
 */
export const MEMBERS_PATH = `${API_BASE}/api/members`;

/** Where a visitor reaches a person. Used by the footer and every stub page,
 *  so it lives here rather than being retyped in nine files. */
export const CONTACT_EMAIL = "support@expressautomate.app";
export const CONTACT_MAILTO = `mailto:${CONTACT_EMAIL}`;
