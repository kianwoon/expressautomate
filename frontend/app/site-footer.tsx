import { Logo } from "./logo";

/**
 * The site footer, shared by the landing page, the stubs and the dashboard.
 *
 * Columns follow the design mockup. Where a destination does not exist yet it
 * points at a stub that says so rather than at nothing — see stub-page.tsx for
 * why that beats a greyed-out label.
 */
const COLUMNS = [
  {
    heading: "Product",
    links: [
      { href: "/use-cases", label: "Use cases" },
      { href: "/#how", label: "How it works" },
      { href: "/#what", label: "What you can do" },
      { href: "/pricing", label: "Pricing" },
    ],
  },
  {
    heading: "Company",
    links: [
      { href: "/about", label: "About us" },
      { href: "/careers", label: "Careers" },
      { href: "/privacy", label: "Privacy" },
      { href: "/terms", label: "Terms" },
    ],
  },
  {
    heading: "Resources",
    links: [
      { href: "/blog", label: "Blog" },
      { href: "/help", label: "Help centre" },
      { href: "/#security", label: "Security" },
      { href: "mailto:hello@expressautomate.app", label: "Contact" },
    ],
  },
] as const;

export function SiteFooter() {
  return (
    <footer>
      <div className="wrap footer-grid">
        <div className="footer-brand">
          <span className="footer-mark">
            <Logo size={30} mono />
            <span className="brand-name">
              express<span className="gradient-text">automate</span>.app
            </span>
          </span>
          <p>AI automation and intelligence for recruitment operations.</p>
        </div>

        {COLUMNS.map((c) => (
          <nav className="footer-col" key={c.heading} aria-label={c.heading}>
            <h2 className="footer-h">{c.heading}</h2>
            <ul>
              {c.links.map((l) => (
                <li key={l.href}>
                  <a href={l.href}>{l.label}</a>
                </li>
              ))}
            </ul>
          </nav>
        ))}

        <div className="footer-where">
          <p>Made for recruitment agencies.</p>
          <p>Built in Singapore.</p>
        </div>
      </div>
    </footer>
  );
}
