"use client";

import { useEffect } from "react";

import { LANDING_PATH } from "../api";
import { useAuth } from "../auth";
import { SiteFooter } from "../site-footer";
import { SiteNav } from "../site-nav";
import { Glossary } from "./glossary";
import { LookbackSetting } from "./lookback";

/**
 * The signed-in settings page.
 *
 * Two unrelated settings, each fetching for itself and failing for itself:
 * how far back we read the inbox, and the glossary of client shorthand. A
 * glossary we cannot read must not take the lookback control down with it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export default function Settings() {
  const auth = useAuth();

  // Same guard as the dashboard: only a real 401 sends you away, and it goes
  // to the landing page rather than straight into a provider redirect — the
  // choice of provider is the user's.
  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  return (
    <>
      <SiteNav />
      <main>
        <section className="hero" style={{ paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            <span className="eyebrow">Settings</span>
            <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
              Settings.
            </h1>
            <h2 className="eyebrow" style={{ marginTop: 22 }}>
              How far back we read your inbox
            </h2>
            {auth.status === "signed-in" ? (
              <LookbackSetting />
            ) : auth.status === "unreachable" ? (
              <p className="lede" style={{ marginTop: 18 }}>
                We could not reach the server. This is not a sign-in problem — your session is
                untouched. Reload the page in a moment.
              </p>
            ) : (
              /* Nothing about the mailbox before the session check resolves. */
              <p className="lede" style={{ marginTop: 18 }}>
                Checking your session.
              </p>
            )}
          </div>
        </section>
        {/* A second setting, and a separate fetch on purpose: a glossary we
            cannot read must not take the lookback control down with it, and
            the reverse. Asked for only once a session is confirmed, so an
            anonymous visitor never triggers a 401 on the way to the landing
            page. */}
        <Glossary enabled={auth.status === "signed-in"} />
      </main>
      <SiteFooter />
    </>
  );
}

