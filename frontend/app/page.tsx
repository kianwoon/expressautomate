import { HeroCta } from "./hero-cta";
import {
  CardCapture,
  CardInsights,
  CardOrganised,
  CardTeam,
  HeroDashboard,
  Icon,
} from "./landing-visuals";
import { SiteFooter } from "./site-footer";
import { SiteNav } from "./site-nav";

/** The promises under the hero copy — what an agency owner is buying. */
const HERO_POINTS = [
  "Capture every role automatically",
  "Free your recruiters from manual work",
  "See your market with real data",
] as const;

/** The three objections an agency raises before reading any further.
 *  The first one is also where the page is honest about scope: Outlook is the
 *  connector that exists today, said as a starting point rather than a limit. */
const TRUST = [
  { label: "Starts with Outlook, read-only", icon: "shield" },
  { label: "No Power Automate required", icon: "bolt" },
  { label: "Built for recruitment agencies", icon: "users" },
] as const;

/** The four cards with insets. Each pairs a claim with a picture of it. */
const PILLARS = [
  {
    title: "Capture every opportunity",
    body: "Nothing is missed and nobody retypes it. New roles are picked up as they arrive and the details you care about are pulled out for you.",
    icon: "inbox",
    figure: CardCapture,
    tone: "blue",
  },
  {
    title: "Keep everything organised",
    body: "Roles, clients, rates and requirements in one place — searchable, filterable, and the same for everyone who opens it.",
    icon: "check",
    figure: CardOrganised,
    tone: "teal",
  },
  {
    title: "Get market insight",
    body: "Which skills are in demand, what clients are hiring for and how salaries are moving — read from your own data, not a benchmark report.",
    icon: "trend",
    figure: CardInsights,
    tone: "indigo",
  },
  {
    title: "Everyone sees the same record",
    body: "Corrections, notes and exports are all kept with the record, so the team works from one version and any change can be traced.",
    icon: "users",
    figure: CardTeam,
    tone: "amber",
  },
] as const;

/**
 * The impact strip. Deliberately not quantified: the product has no customers
 * yet, so "saves 5-10 hours a week" would be a number nobody has measured.
 * These say what changes, and leave the size of it to the reader.
 */
const IMPACT = [
  {
    icon: "clock",
    title: "Less manual work",
    lead: "for every recruiter",
    body: "No more copying details out of messages and tidying them up by hand.",
  },
  {
    icon: "target",
    title: "Respond faster",
    lead: "to your clients",
    body: "Act on new opportunities while they are still worth acting on.",
  },
  {
    icon: "trend",
    title: "Decide with data",
    lead: "not with memory",
    body: "Understand demand, salary movement and the skills that keep recurring.",
  },
  {
    icon: "smile",
    title: "A steadier service",
    lead: "for the people you place",
    body: "Consistent, current information rather than whatever was remembered.",
  },
  {
    icon: "shield",
    title: "Your data stays yours",
    lead: "and only yours",
    // Not "encrypted at rest" flatly: the mailbox token is, the business
    // columns are not, and the privacy page says so. A card that implies
    // otherwise is the kind of claim a security review catches.
    body: "Separated per agency in the database itself, and never shared, resold or used to train AI models.",
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

/**
 * Written to show the shape of the section, not to claim anyone said them.
 *
 * The product has no customers yet, so there are no quotes to print. Every
 * card is labelled an example and the people are described by role rather
 * than given invented names and faces — a made-up "Jasmine Tan, Director"
 * with a stock photo is the kind of thing a design-partner agency notices,
 * and it would cost more trust than the section could ever buy.
 *
 * Replace with real, attributed quotes once there are some, and delete
 * `illustrative` from the section below.
 */
const VOICES = [
  {
    quote: "It is like having an extra team member who never sleeps. We catch everything now.",
    who: "Director, boutique recruitment agency",
  },
  {
    quote: "The insight helps us advise clients better and close roles faster.",
    who: "Managing partner, IT recruitment specialists",
  },
  {
    quote: "We used to live in Excel. Now we live in clarity.",
    who: "Operations manager, search firm",
  },
] as const;

const SECURITY_STRIP = [
  "Microsoft 365 read-only",
  "Tokens encrypted at rest",
  "Every field traceable to its email",
] as const;

export default function Home() {
  return (
    <>
      <SiteNav />

      <main id="top">
        <section className="hero">
          <div className="wrap hero-grid">
            <div>
              <span className="eyebrow">For recruitment agencies of 3–50</span>
              <h1 style={{ marginTop: 14 }}>
                From scattered data
                <br />
                <span className="gradient-text">to clear decisions,</span>
                <br />
                every day.
              </h1>
              <p className="lede" style={{ marginTop: 20 }}>
                What clients pay, which skills keep recurring, which roles never fill — the
                recruitment work your team already does becomes the evidence they act on, instead
                of scattering across mailboxes, spreadsheets and people&rsquo;s heads.
              </p>

              <ul className="ticks">
                {HERO_POINTS.map((p) => (
                  <li key={p}>
                    <Icon name="check" />
                    {p}
                  </li>
                ))}
              </ul>

              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 26 }}>
                <HeroCta />
                <a className="btn btn-secondary" href="#how">
                  <Icon name="play" />
                  See how it works
                </a>
              </div>
            </div>

            <HeroDashboard />
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
              <h2>Built for how recruitment agencies really work</h2>
              <p className="body" style={{ marginTop: 12 }}>
                The work is already happening. expressautomate turns it into something your agency
                owns and can act on.
              </p>
            </div>

            <div className="grid-4 pillars" style={{ marginTop: 36 }}>
              {PILLARS.map((p) => (
                <div className="card pillar" data-tone={p.tone} key={p.title}>
                  <div className="icon">
                    <Icon name={p.icon} />
                  </div>
                  <h3>{p.title}</h3>
                  <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                    {p.body}
                  </p>
                  <p.figure />
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

        <section id="impact">
          <div className="wrap">
            <div className="head-center">
              <h2>The business impact you can feel</h2>
            </div>
            <ul className="impact" style={{ marginTop: 36 }}>
              {IMPACT.map((i) => (
                <li key={i.title}>
                  <span className="impact-ico">
                    <Icon name={i.icon} />
                  </span>
                  <strong>{i.title}</strong>
                  <span className="impact-lead">{i.lead}</span>
                  <span className="impact-body">{i.body}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section id="customers" className="alt">
          <div className="wrap voices-grid">
            <div>
              <h2>What using it should feel like</h2>
              <p className="body" style={{ marginTop: 12 }}>
                We are looking to work with a small number of Singapore recruitment agencies and build
                the product alongside them.
              </p>
              {/* Said once, plainly, rather than hidden in small print. */}
              <p className="illustrative" style={{ marginTop: 16 }}>
                <Icon name="eye" />
                The quotes beside this are written examples of the outcome we are building towards
                — not real customers. We will put real ones here when we have them, with names.
              </p>
            </div>

            <div className="voices">
              {VOICES.map((v) => (
                <figure className="voice" key={v.who}>
                  <span className="voice-mark" aria-hidden="true">
                    &ldquo;
                  </span>
                  <blockquote>{v.quote}</blockquote>
                  <figcaption>
                    {v.who}
                    <span className="voice-tag">Example</span>
                  </figcaption>
                </figure>
              ))}
            </div>
          </div>
        </section>

        <section id="security">
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
                  application code. The token that grants mailbox access is encrypted at rest.
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

      <SiteFooter />
    </>
  );
}
