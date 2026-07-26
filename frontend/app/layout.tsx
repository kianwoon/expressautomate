import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://expressautomate.app"),
  title: "expressautomate.app — AI recruitment operations",
  description:
    "Connect Outlook and turn recruitment email into structured, searchable job data. Read-only Microsoft 365 access. Built for small recruitment agencies.",
  openGraph: {
    title: "expressautomate.app — AI recruitment operations",
    description: "Turn recruitment email into structured recruitment data.",
    url: "https://expressautomate.app",
    type: "website",
  },
  icons: { icon: "/icon.svg" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
