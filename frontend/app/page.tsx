import { HeroCta } from "./hero-cta";
import { Logo } from "./logo";
import { SiteNav } from "./site-nav";

/** The three objections an agency raises before reading any further. */
const TRUST = [
  { label: "Read-only Microsoft 365", icon: "shield" },
  { label: "No Power Automate required", icon: "bolt" },
  { label: "Built for recruitment agencies", icon: "users" },
] as const;

const CAPABILITIES = [
  {
    title: "Every role, captured",
    body: "New recruitment mail is picked up automatically. One email describing four vacancies becomes four job records — company, position, salary, hours, location, requirements — read by meaning, not by matching labels.",
    icon: "inbox",
  },
  {
    title: "Nothing invented",
    body: "If the salary was not stated, it stays unstated. Anything ambiguous goes to a review queue instead of being guessed at, so your team checks exceptions rather than processing every message.",
    icon: "shield",
  },
  {
    title: "A dataset that compounds",
    body: "Every role captured is another row of evidence about what clients pay, which skills keep recurring, and which vacancies will not fill. Daily work becomes the record your competitors discard.",
    icon: "chart",
  },
] as const;

const FEATURES = [
  {
    title: "Email capture",
    body: "Recruitment mail is read from Outlook as it lands, with read-only access. Nothing is sent, moved or deleted.",
    icon: "download",
  },
  {
    title: "AI extraction",
    body: "Roles, salary, requirements and working hours are read from the wording of the email, not from a template it had to match.",
    icon: "sparkle",
  },
  {
    title: "Review queue",
    body: "Anything the model is unsure of is flagged for a human instead of being written into your data as fact.",
    icon: "list",
  },
  {
    title: "Search & export",
    body: "Search across every captured role, see what your pipeline actually contains, and export to Excel in one click.",
    icon: "trend",
  },
] as const;

const STEPS = [
  {
    title: "Sign in with Microsoft",
    body: "Grant read-only access to the mailbox that receives recruitment mail. There is nothing to install and no flow to build.",
  },
  {
    title: "Choose where to start",
    body: "Today, the last few days, or a date you pick. We never pull your whole mailbox. New mail is processed as it lands.",
  },
  {
    title: "Review, search and export",
    body: "Check what needs a human, correct anything wrong, then search across roles or export to Excel when you need it.",
  },
] as const;

const EXTRACTED = [
  { k: "Position", v: "Treasury Support" },
  { k: "Salary", v: "Around SGD 5,500" },
  { k: "Requirements", v: "Around 3 years of banking experience" },
  { k: "Working hours", v: "Approximately 9am–6pm" },
  { k: "Company", v: "Not mentioned", muted: true },
] as const;

const SECURITY_STRIP = [
  "Microsoft 365 read-only",
  "Tokens encrypted at rest",
  "Every field traceable to its email",
] as const;

const FOOTER_LINKS = [
  { href: "/#security", label: "Security" },
  { href: "/#how", label: "How it works" },
  { href: "mailto:hello@expressautomate.app", label: "Contact" },
] as const;

function Icon({ name }: { name: string }) {
  const common = {
    width: 20,
    height: 20,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  switch (name) {
    case "inbox":
      return (
        <svg {...common}>
          <path d="M22 12h-6l-2 3h-4l-2-3H2" />
          <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
        </svg>
      );
    case "shield":
      return (
        <svg {...common}>
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <path d="m9 12 2 2 4-4" />
        </svg>
      );
    case "bolt":
      return (
        <svg {...common}>
          <path d="M13 2 3 14h8l-1 8 10-12h-8z" />
        </svg>
      );
    case "users":
      return (
        <svg {...common}>
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="4" />
          <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
        </svg>
      );
    case "download":
      return (
        <svg {...common}>
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <path d="m7 10 5 5 5-5" />
          <path d="M12 15V3" />
        </svg>
      );
    case "sparkle":
      return (
        <svg {...common}>
          <path d="m12 3 2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z" />
        </svg>
      );
    case "list":
      return (
        <svg {...common}>
          <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
        </svg>
      );
    case "trend":
      return (
        <svg {...common}>
          <path d="M3 17 9 11l4 4 8-8" />
          <path d="M14 4h7v7" />
        </svg>
      );
    case "eye":
      return (
        <svg {...common}>
          <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      );
    case "building":
      return (
        <svg {...common}>
          <path d="M4 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16" />
          <path d="M14 9h4a2 2 0 0 1 2 2v10" />
          <path d="M8 7h2M8 11h2M8 15h2M2 21h20" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <path d="M3 3v18h18" />
          <path d="m19 9-5 5-4-4-3 3" />
        </svg>
      );
  }
}

/**
 * The hero's right-hand column: the same email, before and after.
 *
 * It carries the argument the headline only asserts — that an ordinary,
 * badly-structured recruitment email becomes a record with named fields — so
 * the values here deliberately mirror the "How it works" demo further down,
 * including the one field that stays `Not mentioned` (§15: never fabricated).
 */
function HeroMock() {
  return (
    <div className="mock-pair">
      <div className="mock">
        <div className="mock-head">
          <span className="mock-head-l">
            <span className="mock-badge">
              <Icon name="inbox" />
            </span>
            New mail captured
          </span>
        </div>
        <div className="mock-body">
          <dl className="mock-meta">
            <dt>From</dt>
            <dd>talent@client.example</dd>
            <dt>Subject</dt>
            <dd>Treasury Support — 3 roles</dd>
          </dl>
          <p>Hi, we&rsquo;re hiring for the following roles:</p>
          <ul className="mock-list">
            <li>Treasury Support</li>
            <li>Senior Treasury Analyst</li>
            <li>Payments Specialist</li>
          </ul>
          <p className="muted">Thanks, Talent Acquisition</p>
        </div>
      </div>

      <span className="mock-arrow" aria-hidden="true">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M5 12h14M13 6l6 6-6 6" />
        </svg>
      </span>

      <div className="mock">
        <div className="mock-head">
          <span className="mock-head-l">Extracted role</span>
          <span className="pill">Ready</span>
        </div>
        <div className="mock-body" style={{ paddingTop: 4 }}>
          <div className="rows">
            {EXTRACTED.map((r) => (
              <div className="row" key={r.k}>
                <span className="row-k">{r.k}</span>
                <span className={"muted" in r && r.muted ? "muted" : undefined}>{r.v}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="demo-foot">
          <Icon name="sparkle" />2 more roles in the same email
        </div>
      </div>
    </div>
  );
}

export default function Home() {
  return (
    <>
      <SiteNav sectionLinks />

      <main id="top">
        <section className="hero">
          <div className="wrap hero-grid">
            <div>
              <span className="eyebrow">For Microsoft 365 recruitment agencies</span>
              <h1 style={{ marginTop: 14 }}>
                The roles are already
                <br />
                <span className="gradient-text">in your inbox.</span>
              </h1>
              <p className="lede" style={{ marginTop: 22 }}>
                Recruitment arrives as email — job specs, rates, requirements, buried in forwarded
                threads and pasted adverts. expressautomate reads what lands in Outlook and turns it
                into structured, searchable job records, so your team stops retyping and starts
                seeing the shape of their market.
              </p>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 28 }}>
                <HeroCta />
                <a className="btn btn-secondary" href="#how">
                  See how it works
                </a>
              </div>
            </div>

            <HeroMock />
          </div>

          <div className="wrap" style={{ marginTop: 56 }}>
            <div className="trustbar">
              {TRUST.map((t, i) => (
                <span key={t.label} style={{ display: "contents" }}>
                  {i > 0 && <span className="trust-sep" aria-hidden="true" />}
                  <span className="trust-item">
                    <Icon name={t.icon} />
                    {t.label}
                  </span>
                </span>
              ))}
            </div>
          </div>
        </section>

        <section id="what">
          <div className="wrap">
            <div className="head-center">
              <span className="eyebrow">Why it matters</span>
              <h2 style={{ marginTop: 12 }}>Recruitment data is trapped in email</h2>
            </div>
            <div className="grid-3" style={{ marginTop: 44 }}>
              {CAPABILITIES.map((c) => (
                <div className="card" key={c.title}>
                  <div className="icon">
                    <Icon name={c.icon} />
                  </div>
                  <h3>{c.title}</h3>
                  <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                    {c.body}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="how" className="alt">
          <div className="wrap split">
            <div>
              <span className="eyebrow">How it works</span>
              <h2 style={{ marginTop: 12 }}>
                From email to structured job records in three steps
              </h2>
              <ol className="steps" style={{ marginTop: 28 }}>
                {STEPS.map((s, i) => (
                  <li className="step" key={s.title}>
                    <span className="step-n">{i + 1}</span>
                    <div>
                      <h3>{s.title}</h3>
                      <p className="body" style={{ fontSize: "0.9375rem", marginTop: 4 }}>
                        {s.body}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>

            <div className="demo">
              <div className="demo-head">One email in</div>
              <div className="demo-body">
                <p className="quote">
                  “Our client is looking for someone to support their Treasury desk. Budget is
                  around 5.5k. Around 3 years’ banking experience. Normal hours roughly 9 to 6.”
                </p>
                <div className="rows">
                  {EXTRACTED.map((r) => (
                    <div className="row" key={r.k}>
                      <span className="row-k">{r.k}</span>
                      <span className={"muted" in r && r.muted ? "muted" : undefined}>{r.v}</span>
                    </div>
                  ))}
                  <div className="row">
                    <span className="row-k">Confidence</span>
                    <span>
                      <span className="meter" aria-hidden="true">
                        <span style={{ width: "92%" }} />
                      </span>
                      <span style={{ fontSize: "0.8125rem", color: "var(--ink-500)" }}>
                        92% — every field kept the wording it came from
                      </span>
                    </span>
                  </div>
                </div>
              </div>
              <div className="demo-foot">
                <Icon name="shield" />
                Ready for review
              </div>
            </div>
          </div>
        </section>

        <section id="features">
          <div className="wrap">
            <div className="head-center">
              <span className="eyebrow">What you can do</span>
              <h2 style={{ marginTop: 12 }}>From inbox to intelligence</h2>
            </div>
            <div className="grid-4" style={{ marginTop: 44 }}>
              {FEATURES.map((f) => (
                <div className="card" key={f.title}>
                  <div className="icon">
                    <Icon name={f.icon} />
                  </div>
                  <h3>{f.title}</h3>
                  <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                    {f.body}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="security" className="alt">
          <div className="wrap">
            <span className="eyebrow">Security by design</span>
            <h2 style={{ marginTop: 12 }}>Read-only, and provably so.</h2>
            <div className="grid-3" style={{ marginTop: 36 }}>
              <div className="card">
                <div className="icon">
                  <Icon name="shield" />
                </div>
                <h3>We cannot send mail</h3>
                <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                  We request <strong>Mail.Read</strong> only. expressautomate cannot send, modify or
                  delete email — the permission to do so is never granted.
                </p>
              </div>
              <div className="card">
                <div className="icon">
                  <Icon name="building" />
                </div>
                <h3>Isolated per agency</h3>
                <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                  Each agency&rsquo;s data is separated in the database itself, not only in
                  application code. Access tokens are encrypted at rest.
                </p>
              </div>
              <div className="card">
                <div className="icon">
                  <Icon name="eye" />
                </div>
                <h3>Always traceable</h3>
                <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                  The original email is never discarded. Every extracted field keeps the wording it
                  came from, so any record can be traced back to its source.
                </p>
              </div>
            </div>

            <div className="trustbar" style={{ marginTop: 28 }}>
              {SECURITY_STRIP.map((s, i) => (
                <span key={s} style={{ display: "contents" }}>
                  {i > 0 && <span className="trust-sep" aria-hidden="true" />}
                  <span className="trust-item">
                    <Icon name="shield" />
                    {s}
                  </span>
                </span>
              ))}
            </div>
          </div>
        </section>
      </main>

      <footer>
        <div className="wrap footer-inner">
          <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Logo size={22} mono />
            expressautomate.app — AI recruitment operations
          </span>
          <nav className="footer-links" aria-label="Footer">
            {FOOTER_LINKS.map((l) => (
              <a href={l.href} key={l.href}>
                {l.label}
              </a>
            ))}
          </nav>
          <span>Singapore</span>
        </div>
      </footer>
    </>
  );
}
