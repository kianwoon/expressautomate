/**
 * The illustrations on the landing page: the hero's product panel and the four
 * card insets. Split out of page.tsx so that file stays about what the page
 * says rather than how each picture is drawn.
 *
 * Every figure here is a drawing of the product, not a screenshot of it, and
 * every number in them is invented. That is normal for a product illustration,
 * and it is why each one carries a visible sample tag and an image role: a
 * reader should be able to tell at a glance that 24 roles, or a chart of the
 * skills in demand, is a placeholder rather than a claim about the market.
 *
 * allow-hardcode: the strings below are illustration content and SVG path
 * geometry — sample values in a picture, not configuration or matching rules.
 */

export function Icon({ name }: { name: string }) {
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
    case "clock":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </svg>
      );
    case "target":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
        </svg>
      );
    case "smile":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="M8 14s1.5 2 4 2 4-2 4-2" />
          <path d="M9 9h.01M15 9h.01" />
        </svg>
      );
    case "play":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="m10 8.5 6 3.5-6 3.5z" fill="currentColor" />
        </svg>
      );
    case "check":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" />
          <path d="m8.5 12.2 2.4 2.4 4.6-5" />
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

const STATS = [
  { k: "New roles", v: "24", note: "+18% vs yesterday" },
  { k: "Hot roles", v: "7", note: "High demand" },
  { k: "Client updates", v: "15", note: "Changes and replies" },
  { k: "Time saved", v: "6.4", note: "Hours this week" },
] as const;

const OPPORTUNITIES = [
  {
    role: "Treasury Support",
    client: "Global Bank",
    where: "Raffles Place, SG",
    pay: "SGD 5,500",
    exp: "3y banking exp",
    tag: "New",
  },
  {
    role: "Senior Java Developer",
    client: "FinTech Co.",
    where: "Hybrid",
    pay: "SGD 8,000–9,500",
    exp: "5y exp",
    tag: "New",
  },
  {
    role: "Risk Analyst",
    client: "Insurance MNC",
    where: "City Hall, SG",
    pay: "SGD 6,000",
    exp: "3y exp",
    tag: "Updated",
  },
] as const;

/**
 * The hero's product panel: a morning view of what the platform found.
 *
 * `role="img"` with a label is deliberate. Without it a screen reader walks
 * the whole panel and announces "Time saved 6.4, hours this week" as ordinary
 * page content, reaching the "Sample data" tag only at the very end — so the
 * one caveat that makes the figures honest arrives after the figures. Treating
 * it as a single labelled image says what it is before any number is read.
 */
export function HeroDashboard() {
  return (
    <figure
      className="app"
      role="img"
      aria-label="Illustration of the expressautomate dashboard, showing captured roles and a summary of the day. The figures shown are sample data."
    >
      <div className="app-rail" aria-hidden="true">
        {["inbox", "layers", "list", "trend", "users", "shield"].map((n) => (
          <span key={n}>
            <Icon name={n} />
          </span>
        ))}
      </div>

      <div className="app-body">
        <div className="app-top">
          <div>
            <strong>Good morning, Alex</strong>
            <span className="app-sub">Here is what came in today.</span>
          </div>
          <span className="app-count">
            <b>12</b> New opportunities
          </span>
        </div>

        <div className="app-stats">
          {STATS.map((s) => (
            <div className="app-stat" key={s.k}>
              <span className="app-stat-k">{s.k}</span>
              <span className="app-stat-v">{s.v}</span>
              <span className="app-stat-n">{s.note}</span>
            </div>
          ))}
        </div>

        <div className="app-list-h">Latest opportunities</div>
        <ul className="app-list">
          {OPPORTUNITIES.map((o) => (
            <li key={o.role}>
              <span className="app-avatar" aria-hidden="true">
                <Icon name="building" />
              </span>
              <span className="app-role">
                <strong>{o.role}</strong>
                <span>{o.client}</span>
              </span>
              <span className="app-meta">{o.where}</span>
              <span className="app-meta">{o.pay}</span>
              <span className="app-meta">{o.exp}</span>
              <span className={`app-tag app-tag-${o.tag.toLowerCase()}`}>{o.tag}</span>
            </li>
          ))}
        </ul>

        <div className="app-foot">
          <span>View all opportunities →</span>
          {/* The one honest label on an invented picture. */}
          <span className="app-sample">Sample data</span>
        </div>
      </div>
    </figure>
  );
}

/**
 * Wrapper for the four card figures.
 *
 * Same reasoning as HeroDashboard: these contain invented counts, an invented
 * skills chart and invented names, and "Top in-demand skills" in particular
 * reads as real market data if nothing says otherwise. The visible "Sample"
 * tag says so to a sighted reader; role="img" plus the label says so to
 * everyone else, before the figures are announced.
 */
function Inset({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="inset" role="img" aria-label={`${label}. The figures shown are sample data.`}>
      <span className="inset-sample" aria-hidden="true">
        Sample
      </span>
      {children}
    </div>
  );
}

const CAPTURED = [
  "New role: Treasury Support",
  "Senior Java Developer",
  "Risk Analyst",
  "Business Analyst",
  "Data Engineer",
] as const;

/** Card inset: roles arriving and being recorded one after another. */
export function CardCapture() {
  return (
    <Inset label="Illustration of roles being captured as they arrive">
      <ul className="inset-feed">
        {CAPTURED.map((r, i) => (
          <li key={r} data-lead={i === 0 ? "yes" : undefined}>
            <span className="inset-dot" aria-hidden="true" />
            {r}
          </li>
        ))}
      </ul>
    </Inset>
  );
}

const COUNTS = [
  { k: "Roles", v: "128" },
  { k: "Clients", v: "86" },
  { k: "Open roles", v: "63" },
  { k: "Closed roles", v: "24" },
] as const;

/** Card inset: everything filed and counted in one place. */
export function CardOrganised() {
  return (
    <Inset label="Illustration of roles and clients counted in one place">
      <ul className="inset-counts">
        {COUNTS.map((c) => (
          <li key={c.k}>
            <span className="inset-ico" aria-hidden="true">
              <Icon name="layers" />
            </span>
            {c.k}
            <b>{c.v}</b>
          </li>
        ))}
      </ul>
    </Inset>
  );
}

const SKILLS = [
  { k: "Python", w: 92 },
  { k: "AWS", w: 78 },
  { k: "SQL", w: 64 },
  { k: "Kubernetes", w: 47 },
  { k: "Power BI", w: 33 },
] as const;

/** Card inset: what the accumulated data says about the market. */
export function CardInsights() {
  return (
    <Inset label="Illustration of a chart of skills in demand">
      <span className="inset-h">Top in-demand skills</span>
      <ul className="inset-bars">
        {SKILLS.map((s) => (
          <li key={s.k}>
            <span>{s.k}</span>
            <span className="inset-bar" aria-hidden="true">
              <span style={{ width: `${s.w}%` }} />
            </span>
          </li>
        ))}
      </ul>
    </Inset>
  );
}

const ACTIVITY = [
  { who: "A", what: "Alex corrected a salary", when: "10:15" },
  { who: "M", what: "Maya added a note", when: "10:27" },
  { who: "J", what: "James exported the pipeline", when: "11:02" },
] as const;

/** Card inset: the history of who changed what (§27). */
export function CardTeam() {
  return (
    <Inset label="Illustration of a history of changes made by a team">
      <ul className="inset-activity">
        {ACTIVITY.map((a) => (
          <li key={a.what}>
            <span className="inset-who" aria-hidden="true">
              {a.who}
            </span>
            <span>{a.what}</span>
            <span className="inset-when">{a.when}</span>
          </li>
        ))}
      </ul>
    </Inset>
  );
}
