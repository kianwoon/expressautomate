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

/** One candidate, for GET/PATCH/DELETE. Encoded, so an id that is not the
 *  uuid we expect cannot walk out of its own path segment. */
export function candidatePath(id: string): string {
  return `${CANDIDATES_PATH}/${encodeURIComponent(id)}`;
}

export function candidateArchivePath(id: string): string {
  return `${candidatePath(id)}/archive`;
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

/** Where a visitor reaches a person. Used by the footer and every stub page,
 *  so it lives here rather than being retyped in nine files. */
export const CONTACT_EMAIL = "support@expressautomate.app";
export const CONTACT_MAILTO = `mailto:${CONTACT_EMAIL}`;
