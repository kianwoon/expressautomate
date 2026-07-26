import { Logo } from "./logo";
import { SignupForm } from "./signup-form";
import { SiteNav } from "./site-nav";

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

const STEPS = [
  "Sign in with Microsoft and grant read-only access to the mailbox that receives recruitment mail.",
  "Choose how far back to start — today, the last few days, or a date you pick. We never pull your whole mailbox.",
  "New mail is processed as it lands. Review what needs a human, correct anything wrong, export to Excel when you need it.",
] as const;

const EXTRACTED = [
  { k: "Position", v: "Treasury Support" },
  { k: "Salary", v: "Around SGD 5,500" },
  { k: "Requirements", v: "Around 3 years of banking experience" },
  { k: "Working hours", v: "Approximately 9am–6pm" },
  { k: "Company", v: "Not mentioned", muted: true },
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
  };
  if (name === "inbox") {
    return (
      <svg {...common}>
        <path d="M22 12h-6l-2 3h-4l-2-3H2" />
        <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
      </svg>
    );
  }
  if (name === "shield") {
    return (
      <svg {...common}>
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        <path d="m9 12 2 2 4-4" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M3 3v18h18" />
      <path d="m19 9-5 5-4-4-3 3" />
    </svg>
  );
}

export default function Home() {
  return (
    <>
      <SiteNav sectionLinks />

      <main id="top">
        <section className="hero">
          <div className="wrap">
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
              <a className="btn btn-primary" href="#start">
                Request early access
              </a>
              <a className="btn btn-secondary" href="#how">
                See how it works
              </a>
            </div>
          </div>
        </section>

        <section id="what">
          <div className="wrap">
            <span className="eyebrow">What it does</span>
            <h2 style={{ marginTop: 12, maxWidth: "18ch" }}>
              Recruitment operations, handled
            </h2>
            <div className="grid-3" style={{ marginTop: 36 }}>
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
              <h2 style={{ marginTop: 12 }}>Connect Outlook. That is the setup.</h2>
              <ol className="steps" style={{ marginTop: 28 }}>
                {STEPS.map((s, i) => (
                  <li className="step" key={s}>
                    <span className="step-n">{i + 1}</span>
                    <p className="body" style={{ fontSize: "0.9375rem" }}>
                      {s}
                    </p>
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
                </div>
              </div>
            </div>
          </div>
        </section>

        <section id="security">
          <div className="wrap">
            <span className="eyebrow">Security</span>
            <h2 style={{ marginTop: 12 }}>Read-only, and provably so.</h2>
            <div className="grid-3" style={{ marginTop: 36 }}>
              <div className="card">
                <h3>We cannot send mail</h3>
                <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                  We request <strong>Mail.Read</strong> only. expressautomate cannot send, modify or
                  delete email — the permission to do so is never granted.
                </p>
              </div>
              <div className="card">
                <h3>Isolated per agency</h3>
                <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                  Each agency&rsquo;s data is separated in the database itself, not only in
                  application code. Access tokens are encrypted at rest.
                </p>
              </div>
              <div className="card">
                <h3>Always traceable</h3>
                <p className="body" style={{ marginTop: 8, fontSize: "0.9375rem" }}>
                  The original email is never discarded. Every extracted field keeps the wording it
                  came from, so any record can be traced back to its source.
                </p>
              </div>
            </div>
          </div>
        </section>

        <section id="start" className="alt">
          <div className="wrap">
            <span className="eyebrow">Early access</span>
            <h2 style={{ marginTop: 12, maxWidth: "20ch" }}>Start with one mailbox</h2>
            <p className="body" style={{ marginTop: 14, maxWidth: "58ch" }}>
              We are onboarding a small number of agencies while the product is built with them.
              Leave a work email and we will get in touch — no automated mail, no list.
            </p>
            <div style={{ marginTop: 24 }}>
              <SignupForm />
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
          <span>Singapore</span>
        </div>
      </footer>
    </>
  );
}
