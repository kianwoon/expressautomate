import { HeroCta } from "./hero-cta";
import { Logo } from "./logo";
import { SiteNav } from "./site-nav";

/** The three objections an agency raises before reading any further.
 *  The first one is also where the page is honest about scope: Outlook is the
 *  connector that exists today, said as a starting point rather than a limit. */
const TRUST = [
  { label: "Starts with Outlook, read-only", icon: "shield" },
  { label: "No Power Automate required", icon: "bolt" },
  { label: "Built for recruitment agencies", icon: "users" },
] as const;

const CAPABILITIES = [
  {
    title: "One source of truth",
    body: "Everything the platform consumes lands in the same structured record — company, position, salary, hours, location, requirements — read by meaning rather than by matching labels. Not another silo for your team to remember to check.",
    icon: "layers",
  },
  {
    title: "Nothing invented",
    body: "If the salary was not stated, it stays unstated. Anything ambiguous goes to a review queue instead of being guessed at, so your team checks exceptions rather than processing everything by hand.",
    icon: "shield",
  },
  {
    title: "Insight that compounds",
    body: "The more it consolidates, the sharper the picture: what clients actually pay, which skills keep recurring, which roles never fill. Daily operations become the asset your competitors throw away.",
    icon: "chart",
  },
] as const;

const FEATURES = [
  {
    title: "Connect your sources",
    body: "Point it at where recruitment work already arrives. Access is read-only — nothing is ever sent, moved or deleted on your behalf.",
    icon: "download",
  },
  {
    title: "AI extraction",
    body: "Roles, salary, requirements and working hours are read from how people actually write, not from a template they had to match.",
    icon: "sparkle",
  },
  {
    title: "Review queue",
    body: "Anything the model is unsure of is flagged for a human instead of being written into your data as fact.",
    icon: "list",
  },
  {
    title: "Insight & export",
    body: "Search everything at once, see the rates, skills and client patterns underneath it, and export to Excel when you need to.",
    icon: "trend",
  },
] as const;

const STEPS = [
  {
    title: "Connect your first source",
    body: "Sign in with Microsoft and grant read-only access to the mailbox recruitment work arrives at. Nothing to install, no flow to build.",
  },
  {
    title: "Let the AI structure it",
    body: "Choose how far back to start — today, the last few days, or a date you pick. From then on it keeps up on its own, and everything lands in one record.",
  },
  {
    title: "Act on one consolidated view",
    body: "Search across everything at once, see what the numbers say about your market, correct the handful of things that need a human, and export when you need to.",
  },
] as const;

/**
 * What the platform consumes. Outlook is the only one that exists today and
 * says so; the rest are marked planned rather than shown as a row of logos
 * that implies otherwise — the same rule the extraction itself follows (§15):
 * state what is known, mark what is not.
 *
 * Every planned entry is something the plan actually commits to (§36 Phase 2),
 * so this list stays a roadmap rather than a wish.
 */
const CONNECTORS = [
  { name: "Outlook", detail: "Recruitment mail, read-only", state: "live" },
  { name: "Documents", detail: "PDF and Word attachments", state: "planned" },
  { name: "Excel trackers", detail: "The sheets you keep today", state: "planned" },
  { name: "Your ATS", detail: "Where placements are recorded", state: "planned" },
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
    case "layers":
      return (
        <svg {...common}>
          <path d="m12 2 9 5-9 5-9-5z" />
          <path d="m3 12 9 5 9-5" />
          <path d="m3 17 9 5 9-5" />
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
            What comes in
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
          <span className="mock-head-l">One consolidated record</span>
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
          <Icon name="sparkle" />2 more roles found in the same source
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
              <span className="eyebrow">For recruitment agencies of 3–50</span>
              <h1 style={{ marginTop: 14 }}>
                Your agency knows more than any one person can see.
                <br />
                <span className="gradient-text">Put it in one place.</span>
              </h1>
              <p className="lede" style={{ marginTop: 22 }}>
                Recruitment knowledge is scattered — across mailboxes, spreadsheets, job specs and
                people&rsquo;s heads. expressautomate consolidates it into one structured, searchable
                record of your market: what clients actually pay, which skills keep recurring, which
                roles never fill. Your recruiters stop retyping and remembering, and start acting on
                evidence.
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

          <div className="wrap" style={{ marginTop: 40 }}>
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
              <h2 style={{ marginTop: 12 }}>Scattered data cannot be acted on</h2>
            </div>
            <div className="grid-3" style={{ marginTop: 32 }}>
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
              <h2 style={{ marginTop: 12 }}>Connect a source. Get a picture.</h2>
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

              <ul className="connectors">
                {CONNECTORS.map((c) => (
                  <li className="connector" key={c.name} data-state={c.state}>
                    <span className="connector-dot" aria-hidden="true" />
                    <span>
                      <strong>{c.name}</strong>
                      <span className="connector-detail">{c.detail}</span>
                    </span>
                    <span className="connector-state">
                      {c.state === "live" ? "Live" : "Planned"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="demo">
              <div className="demo-head">What one message becomes</div>
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
              <h2 style={{ marginTop: 12 }}>From scattered work to one clear picture</h2>
            </div>
            <div className="grid-4" style={{ marginTop: 32 }}>
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
            <div className="grid-3" style={{ marginTop: 28 }}>
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
