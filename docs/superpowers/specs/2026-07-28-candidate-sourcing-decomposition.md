# Work history and sourcing, in five pieces

Decided 2026-07-28. A decomposition, not a design — each piece below gets its
own spec when it is reached.

The goal is one sentence: **when a job order arrives, the platform should be
able to say which of this agency's candidates are worth putting in front of the
client.** Today it cannot, and the reason is not the absence of a matching
algorithm. It is the absence of anything to match on.

## What exists today

A candidate is one flat row (`app/models/candidate.py:40-136`): a current
title, a current employer, and `years_experience` as a single scalar. There is
no record of where someone worked before, for how long, or in what order.

The two files with `matching` in their names do not match candidates to jobs.
`candidate_matching.py:58` resolves *identity* — is this email or phone the
person we already have — and `client_matching.py:94` does the same for company
names. Neither has any notion of fit, and nothing else in the codebase does
either.

So a matching engine built now would rank on current title, current employer
and one integer. That is not sourcing; it is sorting.

## Why five pieces and not one project

Work history is a data model that people fill in. Sourcing is a ranking engine
that reads it. Between them sit three separate ways of getting history into the
system, two of which need machinery that does not exist yet — attachment
storage and a CV parser.

Written as one spec, that is a document too large to implement from, whose
later half would be stale before it was built.

| # | Piece | Needs | Status |
|---|---|---|---|
| 1 | History model, and a recruiter typing into it | — | **specced** |
| 2 | CV upload and AI parse | 1, attachment storage | not started |
| 3 | CVs from email attachments | 1, 2, Graph attachment handling | not started |
| 4 | History rows in the spreadsheet import | 1, the import in [candidate profiles](2026-07-28-candidate-profiles-design.md) | not started |
| 5 | Sourcing a job order | 1 — but 2–4 are what make it good | not started |

Piece 1 is the only one everything else requires. Pieces 2 and 3 share a
parser; 3 is largely 2 pointed at a different inbox. Piece 5 is buildable the
day piece 1 ships, and worth building only once history is populated.

## The interface between them

All four ingress paths write the same table. They differ only in two columns,
and those columns are why the table can absorb them without redesign:

- **`source`** — `human`, `cv_upload`, `email_attachment`, `import`.
- **`status`** — `unconfirmed`, `confirmed`, `rejected`.

A row typed by a recruiter is `confirmed` the moment it is saved. A row a model
produced arrives `unconfirmed`, carrying its `extraction_id`, and waits for a
person. This is the shape the clients screen already has — ingestion proposes,
a human confirms — and it is the only arrangement consistent with §15: the AI
never silently rewrites what a person asserted.

Piece 1 ships both columns and only ever writes `human` / `confirmed`. Adding
them later would mean a migration over live rows in every tenant.

## What piece 5 will be able to use

Recording this now so the model in piece 1 is not shaped by guesswork about
matching:

- **Tenure and progression** — how long in each role, and whether titles
  ascend. Derivable from role spans alone.
- **Employer overlap** — a candidate who worked at the hiring company, or at a
  named competitor, from `employer_normalized`.
- **Recency** — experience from 2011 is not experience from last year.
- **Industry adjacency** — via employer, once client profiles carry industry.

An opportunity already offers `skills` as an array, plus `salary_min/max`,
`location_normalized`, `seniority`, `job_family` and `employment_type`
(`app/models/opportunity.py:26-73`). Its `requirements` and `job_description`
are free text and stay that way; piece 5 matches on the structured fields or
parses those two itself. That is piece 5's problem, not piece 1's.

## What this decomposition does not decide

Whether sourcing ranks by rules or by a model, whether shortlists are stored,
and how a submission to a client is tracked. Those belong to piece 5's spec.
§38 Phase 4 of the implementation plan lists "candidate/job matching" and
"submission tracking" together; this decomposition deliberately separates them,
because a ranked list is useful before anything tracks what was sent.
