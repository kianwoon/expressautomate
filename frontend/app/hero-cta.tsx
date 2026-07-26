"use client";

import { DASHBOARD_PATH } from "./api";
import { useAuth } from "./auth";

/**
 * The hero's primary call to action.
 *
 * Same problem as the nav's auth corner (see site-nav.tsx): this is a static
 * export, so the HTML is written at build time and cannot know who is signed
 * in. "Request early access" is a lie to someone who already has an account,
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
 * "unreachable" and "anonymous" both fall back to the early-access button: it
 * only offers an action and asserts nothing, and offering early access to
 * someone who turns out to have an account is a far smaller error than hiding
 * the sign-up from a genuine visitor because our API blipped.
 */
export function HeroCta() {
  const auth = useAuth();
  const resolved = auth.status !== "loading";

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
        <a className="btn btn-primary" href="#start">
          Request early access
        </a>
      )}
    </div>
  );
}
