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

/** POST the chosen period. The only thing that starts ingestion. */
export const MAILBOX_INGEST_PATH = `${API_BASE}/api/mailbox/ingest`;

/** Who is signed in: 200 with user/tenant/mailbox, or 401 when nobody is. */
export const ME_PATH = `${API_BASE}/api/auth/me`;

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

/** Where a visitor reaches a person. Used by the footer and every stub page,
 *  so it lives here rather than being retyped in nine files. */
export const CONTACT_EMAIL = "hello@expressautomate.app";
export const CONTACT_MAILTO = `mailto:${CONTACT_EMAIL}`;
