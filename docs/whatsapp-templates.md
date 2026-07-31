# WhatsApp message templates

Four templates to submit in Meta Business Manager → WhatsApp Manager →
Message templates. **None are configured today** — all four settings default
to `""` ([config.py:649-657](../backend/app/core/config.py)) and none is set on
any Koyeb service, so no WhatsApp notification has ever been sent.

**The parameter count and order are a contract with the code.** `{{1}}` is the
first item in `body_params`, and so on. Change the order in
`app/services/notify/render.py` without resubmitting the template and you get a
delivered message that reads as a job title at a company that does not exist.
Change the count either way and Meta rejects every send.

Meta's rules that shaped the copy below, all of which reject a template
outright: a body may not begin or end with a variable, two variables may not
sit next to each other, and variables must be numbered from 1 with no gaps.

Every parameter passes through `_or_missing`, so a missing value arrives as the
literal `Not mentioned` rather than a blank — Meta rejects blank parameters.
The copy has to read sensibly when a line says `Not mentioned`, which is why
each variable sits behind a label rather than inside a sentence.

Category is **Utility** for the first three: they are triggered by something a
colleague did, not by marketing. A Marketing template needs separate opt-in and
is the first thing Meta throttles.

---

## 1. `ea_job_order` → `WHATSAPP_TEMPLATE_OPPORTUNITY_NEW`

- **Category:** Utility · **Language:** English
- **Sent for three kinds:** `opportunity.new`, `opportunity.shared`,
  `opportunity.assigned` — all four parameters are identical, so one approved
  template serves all three.

**The copy is deliberately neutral, and that is the whole design of it.** One
template covering three events cannot say "New job order", because two of the
three are not new — a forwarded vacancy announced as new is a lie to the
recruiter reading it. "Job order update" is true of all three. The precise
headline still reaches them on Telegram, which takes no templates.

**Header:** `Job order update`

**Body:**

```
A job order needs your attention.

Role: {{1}}
Client: {{2}}
Location: {{3}}
Salary: {{4}}

Open it below to see who it is assigned to and what changed.
```

| Variable | Value | Sample for approval |
|---|---|---|
| `{{1}}` | job title | `Senior Backend Engineer` |
| `{{2}}` | client name | `Acme Pte Ltd` |
| `{{3}}` | location | `Singapore` |
| `{{4}}` | salary | `SGD 8,000 - 10,000` |

**Button:** type *Visit website*, dynamic —
`https://expressautomate.app/dashboard?o={{1}}`

---

## 2. `ea_job_order_review` → `WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW`

- **Category:** Utility · **Language:** English · **Kind:**
  `opportunity.needs_review`

Same four parameters as above. A separate template because this one asks for an
action rather than reporting one, and merging them would force the neutral
wording onto a message that should be pointed.

**Header:** `Job order needs review`

**Body:**

```
The AI was not confident about this job order, so it is waiting for a human.

Role: {{1}}
Client: {{2}}
Location: {{3}}
Salary: {{4}}

Check the extracted details before it goes any further.
```

Variables and samples as in template 1.

**Button:** type *Visit website*, dynamic —
`https://expressautomate.app/dashboard?o={{1}}`

---

## 3. `ea_candidate_update` → `WHATSAPP_TEMPLATE_CANDIDATE_UPDATE`

- **Category:** Utility · **Language:** English
- **Sent for all six candidate kinds:** shared, assigned, unclaimed, access
  requested, access granted, access declined.

**`{{1}}` is the headline itself**, which is what lets one template serve six
events honestly — the message names its own occasion instead of guessing.
`app/services/notify/render.py` supplies it from `_HEADLINE`, so the wording
below is what the recruiter actually sees.

**Header:** `Candidate update`

**Body:**

```
{{1}} — here are the details.

Candidate: {{2}}
From: {{3}}
Note: {{4}}

Open the candidate to act on it.
```

| Variable | Value | Sample for approval |
|---|---|---|
| `{{1}}` | the headline | `Candidate shared with you` |
| `{{2}}` | candidate name | `Wei Ming Tan` |
| `{{3}}` | who did it | `Sarah Lim` |
| `{{4}}` | their note | `She is open to a move, worth a call` |

The six values `{{1}}` can take, verbatim from `_HEADLINE`:

`Candidate shared with you` · `Candidate handed over to you` ·
`Candidate released to the queue` · `Candidate access requested` ·
`Candidate access granted` · `Candidate access declined`

**`{{3}}` and `{{4}}` will often say `Not mentioned`.** The delivery worker
rebuilds the event from the outbox row, and neither the actor nor the note is
stored there — a known gap, recorded in the design docs. Labelled lines keep
that readable rather than mangling a sentence.

**Button:** type *Visit website*, dynamic —
`https://expressautomate.app/dashboard/candidates?c={{1}}`

---

## 4. `ea_link_code` → `WHATSAPP_TEMPLATE_LINK_CODE`

- **Category: Authentication** — not Utility. Meta supplies the body for this
  category; you do not write it. Pick **Copy code** as the button type, which
  is what makes one-tap entry work in the WhatsApp app.
- One body variable: the code. `app/api/notifications.py:220-226` sends the
  same code as both the body parameter and the button parameter, which is what
  an authentication template expects.

Meta will render it as roughly: `{{1}} is your verification code.` with a copy
button. Nothing to design.

---

## After approval

Approval usually takes minutes to a day. **Set all four on all three services**
— `api`, `worker` and `arq`. Per-service env vars are set by hand and are not
in this repo, which is exactly how `GRAPH_BASE_URL` and `R2_*` came to be
missing on one service and not another:

```bash
for svc in api worker arq; do
  koyeb service update expressautomate/$svc \
    --env WHATSAPP_TEMPLATE_OPPORTUNITY_NEW=ea_job_order \
    --env WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW=ea_job_order_review \
    --env WHATSAPP_TEMPLATE_CANDIDATE_UPDATE=ea_candidate_update \
    --env WHATSAPP_TEMPLATE_LINK_CODE=ea_link_code
done
```

WhatsApp also needs `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN` and
`WHATSAPP_API_BASE_URL`, none of which are set on any service either. The
template names alone will not send anything.

## Known limitation, worth knowing before you wonder

**The buttons land on the list page, not the record.** The frontend is a static
export; `/dashboard/candidates` reads only `?eligible_for=`
([page.tsx:138](../frontend/app/dashboard/candidates/page.tsx)) and `/dashboard`
reads no query parameter at all. The id rides along in the URL and is ignored,
so a recruiter arrives at the right screen and finds the record themselves.

That is fine for a first cut and needs no template change to fix later — Meta
approves the URL prefix, and teaching the pages to read `?c=` and `?o=` is a
frontend change alone.
