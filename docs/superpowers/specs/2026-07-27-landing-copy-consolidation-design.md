# Landing page copy — from "email automation" to data consolidation

**Date:** 2026-07-27
**Status:** approved
**Scope:** copy and one new component on `frontend/app/page.tsx`. No change to
layout, routing, auth, or the backend.

## Why

The landing page sold the mechanism, not the product. "Email", "mail" and
"inbox" appeared about fifteen times, including the H1, which framed
expressautomate as Outlook automation. The governing plan says the opposite in
as many words (§1):

> The long-term objective is not simply email automation. The strategic asset
> is the **continuously growing recruitment dataset** created from daily
> operational activity.

Outlook is connector number one, not the product. The page has to say that.

## Positioning

| | Before | After |
|---|---|---|
| Category | Turns your email into job records | Consolidates the agency's scattered operational data into one structured picture it can act on |
| Lead promise | The roles are in your inbox | Your agency knows more than any one person can see — put it in one place |
| Outlook's role | The product | The first source |

Three claims, in order: **consolidation** (one record instead of five silos),
**insight** (rates, demand, client behaviour), **people freed** (recruiters act
on evidence instead of retyping).

## The honesty constraint

Outlook ingestion is the only data path that exists, and Stage 1 is all that
has shipped. The promise may be source-agnostic; the **present tense may not
be**. Every source claim is written as a starting point rather than a live
capability:

- trust bar reads "Starts with Outlook, read-only", not "connects to your
  stack";
- the connector strip marks Outlook **Live** and everything else **Planned**,
  visibly, rather than showing a row of logos that implies all are connected;
- planned connectors are limited to ones the plan actually commits to —
  PDF/Word attachment extraction, automated Excel sync and ATS integrations
  are all §36 Phase 2. Nothing is invented for the sake of a fuller row.

This is the same rule the product itself follows for extracted values (§15):
say what is known, mark what is not.

## Changes

**Hero.** Eyebrow drops "Microsoft 365". H1 becomes "Your agency knows more
than any one person can see. / Put it in one place." Lede names the scatter
(mailboxes, spreadsheets, job specs, people's heads) and what consolidation
buys. Mock panel labels lose "mail": `What comes in` / `One consolidated
record`.

**Trust bar.** "Read-only Microsoft 365" → "Starts with Outlook, read-only".

**Why it matters** → "Scattered data can't be acted on": *One source of truth*,
*Nothing invented* (unchanged), *Insight that compounds*.

**How it works** → "Connect a source. Get a picture." Steps become *Connect
your first source* → *AI structures it* → *Act on one consolidated view*.
Gains the connector strip below the steps.

**What you can do** → "From scattered work to one clear picture": *Connect your
sources*, *AI extraction*, *Review queue*, *Insight & export*.

**Security.** Unchanged. `Mail.Read`, "cannot send mail" and "the original is
never discarded" are proof, not positioning; vagueness there costs trust.

**Link preview.** `design/og.svg` carries the old H1 and must be re-rendered
via `design/render.sh`; `layout.tsx` descriptions follow the new positioning.

## New component

`ConnectorStrip` — a row of named sources, each with a state of `live` or
`planned`. Data-driven from one array so adding a connector is one line, and
the planned ones are styled down rather than hidden.

## Success criteria

1. "email"/"mail"/"inbox" drops from about fifteen uses to six, and every
   remaining one does real work: one in the hero lede (naming the silos the
   data is scattered across — that is the problem statement, not positioning),
   two in "how it works" (the setup instruction and the Outlook connector's own
   detail line), three in security (`Mail.Read`, "cannot send mail", "the
   original email is never discarded"). None of them frames the product.
2. No sentence claims a capability that does not exist today.
3. `next build` and `eslint --max-warnings=0` pass; no horizontal overflow at
   375/768/1024/1440px.
4. og.png matches the new headline.
