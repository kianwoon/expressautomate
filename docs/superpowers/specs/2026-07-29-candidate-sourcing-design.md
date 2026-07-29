# Sourcing candidates for a job order

Decided 2026-07-29. Backend and UI. Piece 5 of
[work history and sourcing](2026-07-28-candidate-sourcing-decomposition.md),
and the one the whole programme was for.

A job order arrives. This is the screen that says who the agency already knows
who could fill it.

Pieces 1, 2 and 4 exist to make this possible: candidates now have roles with
dates and precision, skills, and a way to get all of it in from CVs and
spreadsheets. Nothing here would have been worth building against a flat row
carrying one job title.

## Decisions

| Question | Answer |
|---|---|
| How ranking works | Deterministic rules first; a model explains only the top few |
| A protected attribute in the requirements | Ranked anyway, but the attribute never reaches the model |
| When it runs | On demand, when a recruiter asks |
| Who is eligible | Active, not placed, and not already submitted to this client |
| Is the result kept | Yes — a run is a record, not a live query |

## What was verified before designing this

- **`job_family` and `seniority` are deliberately empty.** `opportunity.py:5`
  says they exist for future analytics and "stay empty". Seniority is not
  available as a column and must come from the title or the model.
- **`attribute` on a glossary code is stated, not inferred** (`glossary.py:19`)
  — one of race, nationality, gender, age, religion, marital status.
- **No ranking or scoring of any kind exists.** `candidate_matching.py` and
  `client_matching.py` are identity resolution. This piece owns all of it.

## Ranking

**Deterministic first, because a ranking of people should be reproducible and
explainable.** Each component contributes a named, signed amount; the weights
come from `settings`, never literals.

| Signal | Source |
|---|---|
| Skill overlap | `opportunity.skills` through `normalize_skill`, against `candidate_skills.skill_normalized` |
| Salary fit | expected against `salary_min`/`salary_max`, **periods normalised first** |
| Relevant tenure | union of role spans via `app/services/candidate_tenure.py`, weighted by title similarity |
| Recency | experience from 2011 is not experience from last year |
| Employer signal | worked at this client before — `employer_normalized` against `company_name_normalized` |
| Location, employment type, work arrangement | direct comparison |

**Salary periods are the trap.** A candidate expecting 6,000 monthly against a
job paying 90,000 a year is a good fit; compared naively it is a catastrophic
mismatch. `salary_period` exists on both sides for this reason, and
`opportunity.py:90` has a CHECK keeping the values honest.

**The score is stored as its parts, not as a number.** A recruiter asking why
somebody is third gets an answer without anything being re-run.

### On age

No age, race or gender is stored anywhere. But `years_experience` and role
dates correlate with age, and pretending otherwise would be dishonest.
Experience stays, because it is job-related and stated by the candidate.
Nothing infers an age, nothing displays one, and no component uses one.

## The model pass

Only the top N, N from `settings`. It reads the requirements and says why each
candidate fits, quoting the CV — and **every quote is verified against the
stored text** before it is shown, the same discipline
`app/services/cv/extract.py` already applies. Fast model first, escalating on
low confidence (§32). A candidate whose quote does not verify keeps its
deterministic score and gets no explanation, rather than an unsupported one.

### Keeping a protected attribute away from the model

`OpportunityCode` stores each decoded code with `start_char` and `end_char` —
but **those offsets index the source email, not the extracted `requirements`
field**, so they cannot be used to cut spans out of the text the model sees.
What works is removing the verbatim `code` string, which `OpportunityCode`
also stores.

That catches coded discrimination, which is exactly what the glossary exists
for. It does **not** catch a requirement written in plain words: "female
preferred", spelled out, reaches the model untouched.

So the guard is three layers, and its limit is stated rather than implied:

1. Known coded strings are removed from what the model sees.
2. The prompt instructs the model to ignore any requirement about a protected
   characteristic, and to report it if it encounters one.
3. The recruiter sees the flag the job order already carries, and is told
   plainly that the shortlist ignored that requirement.

A fourth layer belongs in a later piece: detecting plainly-worded
discrimination the glossary cannot, which is a classification problem rather
than a lookup.

## Storage

**`candidate_submissions`** — the smallest thing that answers "has this person
been put in front of this client?": `candidate_id`, `client_id`,
`opportunity_id` (nullable, since a submission may predate the job order),
`submitted_at`, `submitted_by`. No status workflow and no stages; this piece
needs one boolean fact. `pipeline_stage` already has a `submitted` value but it
is candidate-global and cannot say *to whom*.

**`sourcing_runs`** — one per time a recruiter asks: `opportunity_id`, `state`,
`candidates_considered`, `shortlisted`, `model_name`, `prompt_version`,
`created_by`.

**`sourcing_matches`** — `run_id`, `candidate_id`, `score`, `reasons` (the
component breakdown), `explanation` and `explanation_evidence`, both nullable.

All three inherit `TenantScoped` with RLS in the same migration.

### Why a run is kept rather than recomputed

A recruiter shows a client a shortlist. If it silently reorders next week
because somebody's CV was re-parsed, the conversation they had is no longer
reproducible. A stored run means "the list I sent you on Tuesday" still exists,
and the model spend is paid once rather than per view.

## Eligibility

Active, not `placed`, and not already submitted to this client.

`rejected` candidates **are** included: a rejection was against one role and
says nothing about this one. Archived and merged records are excluded, as
everywhere else.

Excluding the already-submitted is what required `candidate_submissions`, and
it only works if a recruiter can record a submission — so that action ships in
this piece, or the exclusion never fires.

## API

A new file, `app/api/sourcing.py`.

- `POST /api/opportunities/{id}/sourcing` → **202**, starts a run.
- `GET /api/opportunities/{id}/sourcing` → the latest run and its matches.
- `GET /api/opportunities/{id}/sourcing/{run_id}` → an earlier run.
- `POST /api/candidates/{id}/submissions` and `DELETE .../{submission_id}`.

Another agency's opportunity is **404, never 403**. The run is an arq job with
a timeout from settings, joining `rescan_stuck` like the CV and import jobs, and
bounded by a per-tenant daily cap the way CV parsing is.

## UI

In the job order detail panel: a "Find candidates" action, then the ranked list
with each score's breakdown and the model's reason. A "Mark submitted" action
per row. Where the job order references a protected attribute, a notice says
plainly that the shortlist ignored that requirement.

## Tests

1. Agency A cannot read, start or delete Agency B's runs, matches or submissions.
2. `placed`, archived and merged candidates are excluded; `rejected` are not.
3. A candidate already submitted to this client is excluded.
4. A monthly expectation is compared correctly against an annual salary.
5. A coded protected-attribute string never appears in the model prompt.
6. A model quote that does not verify against the source is dropped, and the
   candidate keeps its deterministic score.
7. The same inputs produce the same deterministic score.
8. A stored run does not change when candidate data later does.
9. Skills are normalised on both sides before comparison.
10. Recording a submission excludes that candidate from the next run.
11. Deleting a submission puts them back.
12. RLS is enforced on all three new tables.
13. No route escapes `/api`.

## Out of scope

Detecting plainly-worded discrimination the glossary cannot decode. Submission
status workflow beyond the single fact of having been submitted. Ranking
against anything but a job order. Notifying a client, or sending anything
anywhere — this piece produces a list a person acts on.
