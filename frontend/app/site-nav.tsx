"use client";

import { useEffect, useId, useState } from "react";

import {
  DASHBOARD_PATH,
  LANDING_AFTER_SIGN_OUT,
  LANDING_PATH,
  LOGOUT_PATH,
  SETTINGS_PATH,
  SWITCH_ACCOUNT_PATH,
} from "./api";
import { displayNameOf, useAuth, useSignInHref } from "./auth";
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
    // state along with the page. It carries the choose-account marker, because
    // our cookie is the only thing that just got cleared — Microsoft still has
    // a live SSO session, and a plain "Sign in" from here would drop the user
    // straight back into the account they just left.
    window.location.assign(LANDING_AFTER_SIGN_OUT);
  }

  const resolved = auth.status !== "loading";
  const signInHref = useSignInHref();

  // Read in an effect, not during render: this is a static export, so the
  // server-rendered HTML has no location and reading it inline is a hydration
  // mismatch. Starting false means the link is briefly present on the
  // dashboard before being removed — the reverse would flash a missing link on
  // every other page, and a control that vanishes is less alarming than one
  // that appears late where you were looking for it.
  const [onDashboard, setOnDashboard] = useState(false);
  useEffect(() => {
    setOnDashboard(window.location.pathname.replace(/\/$/, "") === DASHBOARD_PATH);
  }, []);

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
              {/* The way back. Signing in hands someone a dashboard and then,
                  from every other page on the site, no route to it — the
                  wordmark goes to the landing page and the only other control
                  is their own name. It sits in the bar rather than inside the
                  menu because returning to your work is not an account
                  setting, and it should not cost a hover to find.

                  Hidden on the dashboard itself: a link to the page you are
                  already on is a dead control, and this nav has room for
                  exactly one thing beside the name. */}
              {!onDashboard && (
                <a className="nav-dash" href={DASHBOARD_PATH}>
                  Dashboard
                </a>
              )}
              <AccountMenu
                name={displayNameOf(auth.me)}
                email={auth.me.user.email}
                signingOut={signingOut}
                onSignOut={signOut}
              />
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
            <a className="btn btn-primary" rel="nofollow" href={signInHref}>
              Sign in
            </a>
          )}
        </div>
      </div>
    </nav>
  );
}

/**
 * The account actions, folded under the signed-in user's own name.
 *
 * A state hook rather than `<details>`. `<details>` opens on click only: its
 * `open` attribute is not something CSS can set, so hover could not drive it
 * without JS anyway, and we would still need JS for Escape and for closing
 * when focus leaves. Having taken the hook for those, `<summary>` buys nothing
 * and costs the ability to say `aria-expanded` on a real button — screen
 * readers announce a disclosure widget where this is an account switcher.
 *
 * Three ways in, because hover alone reaches neither a phone nor a keyboard:
 *
 * - Pointer. `onPointerEnter` opens, but only for `pointerType === "mouse"`.
 *   A tap synthesises a mouseenter immediately before its click, so without
 *   that guard the tap would open the menu and the click would toggle it shut
 *   again — the menu would be unopenable by touch.
 * - Touch and click. The trigger is a real `<button>`, so a tap toggles.
 * - Keyboard. Focus anywhere inside opens it; Tab moves through the items
 *   natively; `onBlur` closes it once focus lands outside; Escape closes it
 *   and hands focus back to the trigger, which is where the user was.
 *
 * No `role="menu"`: that role promises arrow-key navigation and a roving
 * tabindex. This is three links in a box, and Tab already works on those —
 * claiming the role without implementing it is worse than not claiming it.
 *
 * The panel is absolutely positioned, so opening it moves nothing: the nav's
 * height and the auth corner's width are the same open or closed, and the
 * corner's `min-width` (globals.css) keeps signed-out and signed-in the same
 * width as each other.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */
function AccountMenu({
  name,
  email,
  signingOut,
  onSignOut,
}: {
  name: string;
  email: string;
  signingOut: boolean;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  return (
    <div
      className="nav-menu"
      onPointerEnter={(e) => {
        if (e.pointerType === "mouse") setOpen(true);
      }}
      onPointerLeave={(e) => {
        // Not while something inside is focused: a keyboard user who opened
        // the menu should not lose it because the mouse happened to cross it.
        if (e.pointerType !== "mouse") return;
        if (!e.currentTarget.contains(document.activeElement)) setOpen(false);
      }}
      onFocus={() => setOpen(true)}
      onBlur={(e) => {
        // `relatedTarget` is where focus is going. Null means it left the
        // document entirely — the menu should not stay open behind a tab
        // switch either.
        if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(e) => {
        if (e.key !== "Escape" || !open) return;
        setOpen(false);
        // Focus goes back to the trigger, not nowhere. Dropping it to the
        // document body would send the next Tab to the top of the page.
        e.currentTarget.querySelector("button")?.focus();
      }}
    >
      <button
        className="btn btn-secondary nav-menu-trigger"
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((wasOpen) => !wasOpen)}
      >
        <span className="nav-who">{name}</span>
        <span className="nav-menu-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {/* Rendered only when open. Kept in the DOM and merely hidden, its links
          would still be in the tab order, so a keyboard user would tab into an
          invisible menu. */}
      {open && (
        <div className="nav-menu-panel card" id={panelId}>
          {/* The address lives here rather than in a `title` on the trigger.
              A native tooltip appears under the cursor after a second —
              directly over the path from the trigger to these items, on the
              way to clicking one — and it is unreachable by keyboard and on
              touch. In the panel it is simply visible whenever the menu is
              open, which is when knowing which account this is matters. */}
          <p className="nav-menu-who">{email}</p>
          <a className="nav-menu-item" href={SETTINGS_PATH}>
            Settings
          </a>
          {/* A plain link, and deliberately not "sign out, then sign in":
              abandoning Microsoft's picker leaves the current session
              untouched, whereas clearing the cookie first would strand someone
              who changed their mind. A completed switch overwrites the session
              cookie in the callback anyway. */}
          <a className="nav-menu-item" rel="nofollow" href={SWITCH_ACCOUNT_PATH}>
            Use a different account
          </a>
          <button
            className="nav-menu-item"
            type="button"
            onClick={onSignOut}
            disabled={signingOut}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      )}
    </div>
  );
}
