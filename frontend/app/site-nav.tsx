"use client";

import { useState } from "react";

import { LANDING_PATH, LOGOUT_PATH, SIGN_IN_PATH } from "./api";
import { displayNameOf, useAuth } from "./auth";
import { Logo } from "./logo";

/** Landing-page section links; the dashboard has no sections to jump to. */
const SECTION_LINKS = [
  { href: "/use-cases", label: "Use cases" },
  { href: "/#what", label: "Benefits" },
  { href: "/#how", label: "How it works" },
  { href: "/#security", label: "Security" },
  { href: "/pricing", label: "Pricing" },
] as const;

/**
 * The whole nav, shared by the landing page and the dashboard.
 *
 * Avoiding the flash: this is a static export, so the HTML is written at build
 * time and cannot know who is signed in — the auth-dependent corner MUST start
 * in some state and correct itself once /api/auth/me answers. Rather than
 * render "Sign in" and swap it (a visible lie, and a width change that shoves
 * the nav around), the slot renders the signed-out markup immediately but with
 * `visibility: hidden` until the answer arrives. The space is reserved and the
 * geometry is final from the first paint, so nothing reflows; only the opacity
 * of an already-laid-out box changes. `.nav-auth` also carries a min-width so
 * the signed-out button and the signed-in identity do not resize each other.
 *
 * If the API is unreachable we fall back to showing "Sign in" — it is the safe
 * default for a nav (it only offers an action, it asserts nothing), and the
 * dashboard handles the unreachable case properly on its own.
 */
export function SiteNav({ sectionLinks = false }: { sectionLinks?: boolean }) {
  const auth = useAuth();
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    setSigningOut(true);
    try {
      await fetch(LOGOUT_PATH, { method: "POST", credentials: "include" });
    } catch {
      // Even if the call failed, leave for the landing page: staying on a
      // signed-in view after the user asked to leave is the worse outcome.
    }
    // A full navigation, not a client route: it discards all in-memory auth
    // state along with the page.
    window.location.assign(LANDING_PATH);
  }

  const resolved = auth.status !== "loading";

  return (
    <nav className="nav">
      <div className="nav-inner">
        <a className="brand" href={LANDING_PATH}>
          <Logo size={46} />
          <span>
            <span className="brand-name">
              express<span className="gradient-text">automate</span>.app
            </span>
            <span className="brand-tag">AI recruitment operations</span>
          </span>
        </a>
        {/* Section links and the auth corner are siblings rather than nested,
            so a phone can put them on separate rows: the auth button stays
            beside the wordmark and the links become their own scrollable
            strip underneath. Nested, the links could only be hidden. */}
        {sectionLinks && (
          <div className="nav-links">
            {SECTION_LINKS.map((l) => (
              <a href={l.href} key={l.href}>
                {l.label}
              </a>
            ))}
          </div>
        )}
        <div
          className="nav-auth"
          data-resolved={resolved ? "yes" : "no"}
          aria-live="polite"
          aria-busy={!resolved}
        >
          {auth.status === "signed-in" ? (
            <>
              <span className="nav-who" title={auth.me.user.email}>
                {displayNameOf(auth.me)}
              </span>
              <button
                className="btn btn-secondary"
                type="button"
                onClick={signOut}
                disabled={signingOut}
              >
                {signingOut ? "Signing out…" : "Sign out"}
              </button>
            </>
          ) : (
              /* A full page load, not a client route: the API answers with a
                 redirect to Microsoft, which a Next link would not follow.

                 rel="nofollow" discourages Chrome's link-preloading heuristics
                 from prerendering this endpoint — a speculative GET here starts
                 a whole OAuth flow. It is a hint only: no HTML attribute
                 reliably disables preloading in every browser and setting, so
                 the backend keeps one cookie per flow (app/api/auth.py) and
                 stays correct even when two /login calls race. */
            <a className="btn btn-primary" rel="nofollow" href={SIGN_IN_PATH}>
              Sign in
            </a>
          )}
        </div>
      </div>
    </nav>
  );
}
