import type { Metadata } from "next";

import { SiteFooter } from "./site-footer";
import { SiteNav } from "./site-nav";

/**
 * Shared shell for the Terms and Privacy pages.
 *
 * Separate from StubPage, which exists to say a page is *not* written. These
 * two now say something, so they get real metadata and are indexable — a
 * consent screen and a publisher-verification review both link here, and a
 * `noindex` legal page reads as one nobody stands behind.
 *
 * The provider's registered name and address are stated on both pages rather
 * than only in a footer: a reader agreeing to terms, or asking who holds their
 * data, should not have to hunt for who "we" is.
 */
export const PROVIDER = {
  name: "K2LAB PTE. LTD.",
  address: ["138 Robinson Road", "#26-01 Oxley Tower", "Singapore 068906"],
} as const;

/** One date for both documents, so they can never drift apart silently. */
export const LEGAL_UPDATED = "27 July 2026";

export function LegalPage({
  title,
  summary,
  children,
}: {
  title: string;
  /** The one-paragraph honest version, for someone who will not read on. */
  summary: string;
  children: React.ReactNode;
}) {
  return (
    <>
      <SiteNav />
      <main className="legal">
        <div className="wrap">
          <span className="eyebrow">Last updated {LEGAL_UPDATED}</span>
          <h1 style={{ marginTop: 12 }}>{title}</h1>
          <p className="lede" style={{ marginTop: 18 }}>
            {summary}
          </p>
          <div className="legal-body">{children}</div>
          <p className="legal-who">
            {PROVIDER.name}
            <br />
            {PROVIDER.address.join(", ")}
          </p>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}

export function legalMetadata(name: string, description: string): Metadata {
  return {
    title: `${name} — expressautomate.app`,
    description,
  };
}
