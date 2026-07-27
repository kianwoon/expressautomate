"use client";

import { DASHBOARD_PATH } from "./api";
import { useAuth, useSignInHref } from "./auth";

/**
 * The hero's primary call to action.
 *
 * Same problem as the nav's auth corner (see site-nav.tsx): this is a static
 * export, so the HTML is written at build time and cannot know who is signed
 * in. "Sign in with Microsoft" is wrong for someone who already has a session,
 * but the page must ship *some* label and correct it once /api/auth/me answers.
 *
 * So the same fix, applied to one button instead of a whole slot: the
 * signed-out button is rendered immediately and the slot keeps it
 * `visibility: hidden` until the answer arrives. `.hero-cta` carries a
 * min-width wide enough for either label, so the box is laid out once and the
 * later swap changes no geometry — no flash of the wrong label, and the
 * secondary button beside it never shifts.
 *
 * The secondary "See how it works" button is outside this slot and stays
 * visible throughout, so the hero is never left with no action at all while
 * the check is in flight.
 *
 * "unreachable" and "anonymous" both fall back to the sign-in button: it only
 * offers an action and asserts nothing, and offering sign-in to someone who
 * turns out to already have a session is a far smaller error than hiding the
 * only way in from a genuine visitor because our API blipped.
 */
export function HeroCta() {
  const auth = useAuth();
  const resolved = auth.status !== "loading";
  const signInHref = useSignInHref();

  return (
    <div
      className="hero-cta"
      data-resolved={resolved ? "yes" : "no"}
      aria-live="polite"
      aria-busy={!resolved}
    >
      {auth.status === "signed-in" ? (
        <a className="btn btn-primary" href={DASHBOARD_PATH}>
          Open dashboard
        </a>
      ) : (
        /* A full page load, not a client route: the API answers with a redirect
           to Microsoft, which a Next link would not follow. rel="nofollow"
           discourages link preloading — see the same note in site-nav.tsx. */
        <a className="btn btn-primary" rel="nofollow" href={signInHref}>
          Sign in with Microsoft
        </a>
      )}
    </div>
  );
}
