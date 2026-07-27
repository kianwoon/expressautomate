import type { Metadata } from "next";
import "./globals.css";

const TITLE = "expressautomate.app — AI recruitment operations";

/** Bump when any file in public/ that this page links as an icon is redrawn.
 *  See the note on `icons` below for why this exists. */
const ICON_VERSION = "2";

/* The one line a link preview gets. WhatsApp truncates the title at roughly
   two lines and the description at two more, so this says what the product
   does and stops — no feature list that will be cut mid-word. */
const PREVIEW_DESCRIPTION =
  "AI that consolidates your agency's scattered recruitment data into one structured, searchable picture — so your team acts on evidence instead of retyping.";

/**
 * metadataBase makes every relative URL below absolute in the emitted HTML,
 * which matters more than it looks: WhatsApp, iMessage and Slack all discard
 * a relative og:image, so a preview with a bare "/og.png" shows no card at all.
 *
 * og.png is a real raster file rather than the SVG the site uses for its own
 * mark — no link-preview crawler renders SVG. Its source is design/og.svg;
 * edit that and run design/render.sh if the wording here changes.
 */
export const metadata: Metadata = {
  metadataBase: new URL("https://expressautomate.app"),
  title: TITLE,
  description:
    "AI recruitment intelligence and operations for small agencies. Consolidate scattered recruitment data into one structured, searchable record — rates, skills and client patterns included. Starts with read-only Outlook access.",
  openGraph: {
    siteName: "expressautomate.app",
    title: TITLE,
    description: PREVIEW_DESCRIPTION,
    url: "/",
    type: "website",
    locale: "en_SG",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        alt: "expressautomate.app — your agency knows more than any one person can see. Put it in one place.",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: PREVIEW_DESCRIPTION,
    images: ["/og.png"],
  },
  icons: {
    /* SVG first for browsers that take it (sharp at any size), .ico second as
       the fallback every other client understands, and the 180px PNG for the
       iOS home screen, which ignores both of the others.

       ?v= is a cache buster, and it is not optional. Cloudflare serves these
       paths with `cache-control: max-age=14400`, so redrawing an icon leaves
       the old one at the edge for up to four hours — measured, not assumed:
       after the deploy that replaced the plated favicon, /icon.svg returned
       `cf-cache-status: HIT, age: 1546` with the old file while
       /icon.svg?cb=99 returned the new one. Browsers cache favicons harder
       still. Bump ICON_VERSION whenever public/icon.svg, favicon.ico or
       apple-touch-icon.png changes; the new URL is a different cache key and
       takes effect at once, with no cache purge to remember. */
    icon: [
      { url: `/icon.svg?v=${ICON_VERSION}`, type: "image/svg+xml" },
      { url: `/favicon.ico?v=${ICON_VERSION}`, sizes: "any" },
    ],
    apple: `/apple-touch-icon.png?v=${ICON_VERSION}`,
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
