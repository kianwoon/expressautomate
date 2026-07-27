import type { Metadata } from "next";

import { SiteFooter } from "./site-footer";
import { SiteNav } from "./site-nav";

/**
 * A page the footer links to but which has nothing to say yet.
 *
 * The alternative was a footer of dead or greyed-out labels, which reads as an
 * abandoned site rather than an early one. This says plainly that the page is
 * not written yet and gives the reader somewhere real to go instead — a
 * section of the landing page, or an email address — so a click is never a
 * dead end.
 *
 * It deliberately does NOT invent content. Pages like Privacy and Terms are
 * legal documents; a plausible-looking placeholder is worse than an honest
 * empty one, because a reader may rely on it.
 */
export function StubPage({
  title,
  blurb,
  next,
}: {
  title: string;
  blurb: string;
  /** Where to send someone who arrived here wanting something real. */
  next: { href: string; label: string }[];
}) {
  return (
    <>
      <SiteNav />
      <main className="stub">
        <div className="wrap">
          <span className="eyebrow">Not published yet</span>
          <h1 style={{ marginTop: 12 }}>{title}</h1>
          <p className="lede" style={{ marginTop: 18 }}>
            {blurb}
          </p>
          <div className="stub-next">
            <span className="stub-next-k">In the meantime</span>
            <ul>
              {next.map((n) => (
                <li key={n.href}>
                  <a href={n.href}>{n.label}</a>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}

/** Every stub shares one shape of metadata; only the name changes. */
export function stubMetadata(name: string): Metadata {
  return {
    title: `${name} — expressautomate.app`,
    description: `${name} is not published yet. expressautomate.app is an AI recruitment intelligence and operations platform for small agencies.`,
    // Nothing here is worth indexing until it says something.
    robots: { index: false, follow: true },
  };
}
