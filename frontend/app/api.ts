// Where the API lives, in one place.
//
// The site is a static export, so this is baked in at build time. Empty means
// same-origin — in production one service serves both the site and /api — and
// NEXT_PUBLIC_API_BASE overrides it for local development against a separate
// backend.
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/** Starts the Microsoft OAuth flow; the API redirects on to Microsoft. */
export const SIGN_IN_PATH = `${API_BASE}/api/auth/microsoft/login`;

export const EARLY_ACCESS_PATH = `${API_BASE}/api/early-access`;

/** Who is signed in: 200 with user/tenant/mailbox, or 401 when nobody is. */
export const ME_PATH = `${API_BASE}/api/auth/me`;

/** Clears the session cookie. POST only. */
export const LOGOUT_PATH = `${API_BASE}/api/auth/logout`;

/** The landing page — where a signed-out visitor is sent from a guarded route. */
export const LANDING_PATH = "/";

/** The signed-in shell. A site route, not an API one — no API_BASE prefix. */
export const DASHBOARD_PATH = "/dashboard";
