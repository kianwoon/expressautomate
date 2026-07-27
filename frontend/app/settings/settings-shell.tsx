"use client";

import { useEffect, useState } from "react";

import { LANDING_PATH, SETTINGS_GLOSSARY_PATH, SETTINGS_NOTIFICATIONS_PATH, SETTINGS_PATH } from "../api";
import { useAuth } from "../auth";
import { SiteFooter } from "../site-footer";
import { SiteNav } from "../site-nav";

/**
 * The frame every settings route renders inside.
 *
 * The auth guard lived in `settings/page.tsx` and is duplicated again in
 * `dashboard/page.tsx`. Splitting settings into three routes would have made
 * four copies of a piece of reasoning that is easy to get subtly wrong — the
 * important part being that `unreachable` is NOT `anonymous`, so an outage of
 * ours never pushes a signed-in user off a guarded page. One copy here now
 * covers all three settings routes. The dashboard keeps its own, since it is
 * not a settings route.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

type Tab = "inbox" | "glossary" | "notifications";

const TABS: { key: Tab; label: string; href: string }[] = [
  { key: "inbox", label: "Inbox", href: SETTINGS_PATH },
  { key: "glossary", label: "Shorthand", href: SETTINGS_GLOSSARY_PATH },
  { key: "notifications", label: "Notifications", href: SETTINGS_NOTIFICATIONS_PATH },
];

export function SettingsShell({
  heading,
  active,
  children,
}: {
  heading: string;
  active: Tab;
  children: React.ReactNode;
}) {
  const auth = useAuth();

  // Only a real 401 sends you away, and it goes to the landing page rather
  // than straight into a provider redirect — the choice of provider is the
  // user's.
  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  // Once we have confirmed the user is signed in, `children` stays mounted
  // even through a later transient `unreachable` (a blip, a 5xx). The
  // notifications route holds live state in-tree while a Telegram link is in
  // progress — unmounting there discards the panel and the QR while a still-
  // valid link token becomes invisible to the recruiter. Overlaying the
  // "could not reach the server" message on top of the mounted children is
  // simpler than lifting that state up into `page.tsx`: every settings route
  // gets the fix for free, and no route has to know its state might need to
  // survive an auth hiccup it didn't cause.
  const [everSignedIn, setEverSignedIn] = useState(false);
  useEffect(() => {
    if (auth.status === "signed-in") setEverSignedIn(true);
  }, [auth.status]);

  return (
    <>
      <SiteNav />
      <main>
        <section className="hero" style={{ paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            <span className="eyebrow">Settings</span>
            <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
            <nav className="nt-tabs" aria-label="Settings sections">
              {TABS.map((t) => (
                <a
                  key={t.key}
                  href={t.href}
                  className={t.key === active ? "nt-tab nt-tab-on" : "nt-tab"}
                  aria-current={t.key === active ? "page" : undefined}
                >
                  {t.label}
                </a>
              ))}
            </nav>
            {auth.status === "signed-in" || (everSignedIn && auth.status === "unreachable") ? (
              <>
                {auth.status === "unreachable" ? (
                  <p className="lede" style={{ marginTop: 18 }}>
                    We could not reach the server. This is not a sign-in problem — your session is
                    untouched. Reload the page in a moment.
                  </p>
                ) : null}
                {children}
              </>
            ) : auth.status === "unreachable" ? (
              <p className="lede" style={{ marginTop: 18 }}>
                We could not reach the server. This is not a sign-in problem — your session is
                untouched. Reload the page in a moment.
              </p>
            ) : (
              /* Covers both "loading" and the instant before the redirect
                 fires on "anonymous". Nothing about the account is asserted
                 before the check resolves — otherwise a signed-out visitor
                 sees a flash of signed-in chrome on the way out. */
              <p className="lede" style={{ marginTop: 18 }}>
                Checking your session.
              </p>
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
