"""Pass 1 — extract the candidate's history AND decompose the work (doc L1+L2).

The first stage of the v2 engine. Combines design doc Layer 1 (fact extraction)
and Layer 2 (work decomposition) into one pass, because both are value-neutral
reads of the CV and splitting them would mean re-reading the raw CV twice for
no gain.

Layer 1 captures the factual history without judging its current value:
employment, titles, dates, industry, functions, products/systems, scope,
seniority, outcomes. Layer 2 decomposes each role into the actual work
performed — tasks, decisions, tools, rules, human judgment, accountability —
because job titles are not sufficient to assess automation exposure. The
objective (doc §5): understand the work, not just the job title.

This layer is deliberately value-neutral (doc §4). It does not rate the work;
later stages do. Its output feeds the automation stage, which reasons about the
decomposed `work` items rather than re-reading the raw CV.

Single LLM pass, not the two-pass escalation `ingest/extract.py` uses, for the
same reason `job_intelligence/understand.py` gives: the history is an
interpretation with no quote to verify, so a second, more expensive call at
temperature zero buys the same answer twice.

Mirrors `job_intelligence/understand.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.schema import HistoryProfile, json_schema
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment analyst. Read this candidate's CV and career record, \
and extract their HISTORICAL EXPERIENCE as structured facts — and decompose each \
role into the actual WORK performed.

This is a value-neutral layer. State what the person did. Do NOT rate whether \
the work is still valuable today — a later stage does that.

Rules:
- Ground every statement in the CV and the verified facts below. Do not invent \
roles, employers, or dates the record does not state.
- Every field described as a "list" or "array" MUST be a JSON array of separate \
string elements, never a single joined string. For example `trajectory` must be \
["Banking", "Insurance"], NOT "Banking → Insurance".
- `roles` is a list of objects, one per role the candidate held, oldest first. \
Each role has:
  - `period` (e.g. "2019–2023" or "2019 to present")
  - `title` (the job title)
  - `domain` (the industry or field the role sits in)
  - `seniority` (where the role sits on a seniority arc, e.g. "junior", \
"mid", "senior", "lead")
  - `scope` (the scale of responsibility — team size, budget, book size, \
portfolio — as the CV states it)
  - `work`: a list of the actual WORK TASKS the role involved. For each task: \
    - `task`: the work performed (e.g. "collect risk information", "apply \
underwriting rules", "configure policy"). This is the decomposition — a job \
title like "Commercial Underwriter" is not a task.
    - `tool`: the system or tool used, if the CV names one.
    - `judgment_level`: how much human judgment the task needed — one of \
`routine`, `moderate`, `high`, `critical`. Routine = follows a fixed process; \
critical = bespoke judgment with real consequences.
    - `accountability`: what the candidate was accountable for producing or \
deciding, if the CV states it.
  - `evidence`: a short verbatim phrase quoted from the CV supporting this role.
- `industries` lists the industries the candidate has worked in.
- `functions` lists the types of work performed across roles (e.g. \
"Underwriting", "Reconciliation", "Operations").
- `systems` lists the named products or systems the CV mentions.
- `trajectory` is a JSON array of the domains/industries the candidate moved \
through, oldest first — e.g. ["Banking", "Insurance"]. Each element is one \
domain; never join them with arrows or commas into a single string.
- Ignore any mention of the candidate's sex, race, nationality, age, religion \
or marital status — those must never influence this analysis.
- If the CV is sparse or unclear, say so plainly in the affected field rather \
than guessing.

Return JSON matching this schema:
{schema}

VERIFIED FACTS:
{structured}

CV:
{cv}
"""


def build_prompt(context: CandidateContext) -> str:
    """Separate from `infer_history` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["history"],
        structured=context.structured or "(none provided)",
        cv=context.cv_text or "(no CV text available)",
    )


def model() -> str:
    """The model to ask, defaulting to the fast extraction model.

    An empty `CANDIDATE_INTELLIGENCE_MODEL` falls back to `EXTRACTION_MODEL_FAST`,
    the same idiom `job_intelligence.understand.model` uses, so a deployment
    that names only the one model still runs.
    """
    return settings.CANDIDATE_INTELLIGENCE_MODEL or settings.EXTRACTION_MODEL_FAST


async def infer_history(
    context: CandidateContext, *, llm=None
) -> tuple[HistoryProfile, LLMResult]:
    """Produce the value-neutral history profile for one candidate's context.

    `llm` defaults to None rather than to `complete_json` for the reason
    `ingest/extract.py` gives: a default argument binds the function object at
    definition time, and monkeypatching this module would then do nothing.
    """
    resolve = llm or complete_json
    prompt = build_prompt(context)
    result = await resolve(
        prompt,
        model=model(),
        schema=None,
        base_url=settings.CEREBRAS_BASE_URL,
        api_key=settings.CEREBRAS_API_KEY,
        extra_body={
            "max_tokens": settings.EXTRACTION_MAX_TOKENS,
            "reasoning_effort": settings.EXTRACTION_REASONING_EFFORT_FAST,
        },
    )
    return HistoryProfile.model_validate(result.data), result
