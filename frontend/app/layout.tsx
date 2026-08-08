import type { Metadata } from "next";
import "./globals.css";
import "./app.css";
import { ChunkErrorReload } from "./chunk-error-reload";

const TITLE = "expressautomate.app — find a place for each person";

/**
 * Bump when any raster in public/ that this page links is redrawn — the three
 * icons or og.png. See the note on `icons` below for the measurement behind it.
 */
const ASSET_VERSION = "4";

const OG_IMAGE = `/og.png?v=${ASSET_VERSION}`;

/* The one line a link preview gets. WhatsApp truncates the title at roughly
   two lines and the description at two more, so this says what the product
   does and stops — no feature list that will be cut mid-word. */
const PREVIEW_DESCRIPTION =
  "Recruitment operations built on a simple belief — every candidate has strengths worth finding, and a client deserves the pre-work to recommend the right one.";

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
    "AI recruitment operations for Singapore agencies. One record per candidate, the pre-work done before a client asks, and the evidence behind every field. Starts with read-only Outlook access.",
  openGraph: {
    siteName: "expressautomate.app",
    title: TITLE,
    description: PREVIEW_DESCRIPTION,
    url: "/",
    type: "website",
    locale: "en_SG",
    images: [
      {
        url: OG_IMAGE,
        width: 1200,
        height: 630,
        alt: "expressautomate.app — a place for each person, and a client worth building with.",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: PREVIEW_DESCRIPTION,
    images: [OG_IMAGE],
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
       still. og.png is on the same clock and carries the same parameter.
       Bump ASSET_VERSION whenever one of those four files is redrawn; the new
       URL is a different cache key and takes effect at once, with no cache
       purge for anyone to remember. */
    icon: [
      { url: `/icon.svg?v=${ASSET_VERSION}`, type: "image/svg+xml" },
      { url: `/favicon.ico?v=${ASSET_VERSION}`, sizes: "any" },
    ],
    apple: `/apple-touch-icon.png?v=${ASSET_VERSION}`,
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Reloads when the running bundle requests a chunk a newer deploy no
            longer ships — see the component. Renders nothing. */}
        <ChunkErrorReload />
        {children}
      </body>
    </html>
  );
}
