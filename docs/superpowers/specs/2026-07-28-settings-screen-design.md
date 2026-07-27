# Settings screen — giving notifications a home

Decided 2026-07-28. Frontend counterpart to
[the notification system](2026-07-28-notification-system-design.md), whose API
shipped with no interface at all.

The notification backend is live in production — a recruiter can link Telegram,
subscribe to events and receive job orders — but only through three
hand-written API calls. Nothing renders it. Meanwhile `/settings` is already a
single page stacking two unrelated sections, and notifications would be the
third and by far the largest: two channels, a linking flow with its own
verification round-trip, and a per-event subscription matrix.

Adding it to the stack is what makes the page unusable, so the page changes
shape first.

## Decisions

| Question | Answer | Why |
|---|---|---|
| Structure | Separate routes under a shared shell | Each section already fetches and fails independently; routes make that structural rather than conventional. |
| Notifications layout | One card per destination | A destination carries state — verified, disabled, scope — that needs somewhere to live. Two event kinds do not justify a grid. |
| Unconfigured WhatsApp | Shown, disabled, with a reason | The API already reports `channels.whatsapp`; when it flips true the block lights up with no code change. |
| Telegram linking | Link + QR, poll until it lands | The recruiter is on a laptop and Telegram is on their phone. Polling means they never wonder whether it worked. |
| Tenant-wide destinations | Deferred | The endpoint stays. The toggle can appear when someone asks for a shared feed — no schema or API change needed. |
| QR generation | `qrcode` dependency | The first runtime dependency in this frontend, accepted deliberately: QR encoding is fiddly and not worth hand-rolling. |

## Constraints that shaped this

**Static export.** `next.config.ts` sets `output: "export"` and
`trailingSlash: true`. There is no server to redirect, so `/settings` cannot
bounce to `/settings/inbox` without a client-side flash.

**No runtime dependencies.** Before this, `package.json` lists only `next`,
`react` and `react-dom`. That looks deliberate rather than accidental, which is
why adding one is recorded here as a decision rather than made in passing.

**No test framework.** There is no jest, vitest or playwright. This work adds
none — see *Testing* below, which says plainly what that costs.

## Routes

| Route | Content |
|---|---|
| `/settings` | How far back we read your inbox — the existing `LookbackSetting` |
| `/settings/glossary` | The existing `Glossary` |
| `/settings/notifications` | New |

`/settings` keeps the inbox setting rather than redirecting to
`/settings/inbox`. Two reasons: existing links and bookmarks keep working, and
a static export cannot redirect without rendering something first. The
sub-nav labels it "Inbox", so the URL and the label differ slightly — accepted
in exchange for not shipping a redirect that exists only to satisfy symmetry.

### `SettingsShell`

A component wrapping `SiteNav`, the sub-nav, the auth guard and `SiteFooter`.

The auth guard currently lives inside `app/settings/page.tsx`: it redirects to
the landing page on `anonymous`, and distinguishes `unreachable` — where the
session is untouched and the right advice is to reload — from a real sign-out.
Three routes would otherwise carry three copies of that reasoning, and copies
drift. It moves into the shell once.

The shell renders the sub-nav for every settings route, so a recruiter can see
that notifications exist without knowing the URL.

## The notifications page

One `GET /api/notifications/settings` on mount supplies everything: which
channels are configured, which destinations exist with their subscribed event
kinds, and the catalogue of events. Nothing else fetches on load.

Four files, one concern each:

| File | Responsibility |
|---|---|
| `notifications/page.tsx` | Fetch, loading and error states, compose |
| `notifications/add-destination.tsx` | Telegram button; the disabled WhatsApp block |
| `notifications/telegram-link-panel.tsx` | The link, the QR, the polling |
| `notifications/destination-card.tsx` | One destination: state, event checkboxes, unlink |

The split follows the existing settings components, which are one-concern files
of 150–300 lines. It also keeps the polling logic — the only stateful,
timer-driven part of this screen — isolated in a file that does nothing else.

### Subscriptions

A checkbox sends `PUT /api/notifications/subscriptions` with that
destination's **full** new event set.

This matters and is easy to get wrong: the endpoint *replaces* rather than
merges, deliberately, because the screen is the source of truth for which
boxes are ticked and a merge would make unticking impossible. Sending only the
changed kind would silently clear the others.

### The linking flow

1. `POST /api/notifications/destinations/telegram/link` returns a
   `t.me/<bot>?start=<token>` URL and `expires_in_minutes`. The expiry comes
   from that response, never from a constant in the frontend — the backend
   reads it from `NOTIFY_LINK_TOKEN_TTL_MINUTES`, and a duplicated value here
   would disagree with it the first time an operator changed the setting.
2. The panel shows the link and its QR.
3. While the panel is open, the page re-reads the settings endpoint every
   **3 seconds**. Fast enough that pressing Start feels instant, slow enough
   that a fifteen-minute window is 300 requests rather than thousands.
4. A new destination appearing ends the flow. The panel closes and the card
   list re-renders with it.
5. The TTL expiring ends the flow the other way: polling stops and the panel
   offers a fresh link rather than leaving a dead QR on screen.

Polling runs **only** while the panel is open. A settings page sitting idle in
a background tab issues no requests.

The token is single-use and short-lived by design, so a stale QR is
inert rather than dangerous — but showing one that cannot work is still a
thing to avoid, hence step 5.

## Error handling

The shell already distinguishes three states, and the notifications page adds
a fourth:

| State | Behaviour |
|---|---|
| `anonymous` | Redirect to the landing page — never straight into a provider redirect; the choice of provider is the user's |
| `unreachable` | "This is not a sign-in problem — your session is untouched." Reload advice |
| `signed-in` | Render |
| `channels.telegram === false` | Render "not configured" instead of a link button that would 503 |

Unlink asks for confirmation. It is reversible — re-linking restores it — but
silently dropping a destination someone relies on is worse than one extra
click.

A failure in any one route cannot affect another, because they are no longer
in the same tree. That was previously a convention maintained by hand.

## Testing

**This work ships with no automated tests, because the frontend has no test
framework.** Stating that plainly is better than a testing section that
describes nothing.

The specific things that therefore go unverified except by hand:

- polling starts, stops on success, and stops on expiry
- the subscription PUT sends the full set rather than the delta
- the disabled WhatsApp block renders from the `channels` flag rather than
  from a hardcoded false
- the four auth states each render what they should

Introducing vitest and testing-library is a separate piece of work with its own
decisions, and it would set the pattern for the whole frontend rather than for
one screen. It should happen; it should not happen inside this change.

## Out of scope

Tenant-wide destinations and their scope toggle. The WhatsApp opt-in and
verification flow, which is blocked on the WABA regardless. Any test
infrastructure. None of these need a schema or API change when they arrive.
