"""Stage 1 — read the career from the CV and structure it.

The first stage of the engine. Reads the candidate's CV and the verified
structured facts (roles, skills, title) and answers: what is the chronological
career, what domains has the candidate moved through, what is their trajectory
and current stage. Its output feeds the capability stage, so the capability
inference inherits the career's framing rather than re-reading the raw CV.

Single LLM pass, not the two-pass escalation `ingest/extract.py` uses, for the
same reason `job_intelligence/understand.py` gives: the career profile is an
interpretation with no quote to verify, so a second, more expensive call at
temperature zero buys the same answer twice.

Mirrors `job_intelligence/understand.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.schema import CareerProfile, json_schema
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment analyst. Read this candidate's CV and career record, \
and structure the CAREER it describes — not the keywords, the progression.

Rules:
- Ground every statement in the CV and the verified facts below. Do not invent \
roles, employers, or dates the record does not state.
- `timeline` is a list of objects, one per role the candidate held, oldest \
first. Each object has `period` (e.g. "2019–2023" or "2019 to present"), \
`title`, and `domain` (the industry or field the role sits in).
- `trajectory` is the ordered list of domains the candidate moved through, \
oldest first — the arc of their career.
- `primary_domain` is the single field the candidate is most established in. \
`secondary_domains` lists the adjacent fields they have touched.
- `career_direction` is a short phrase describing where the career is heading \
(e.g. "deepening specialisation in reinsurance"). `career_stage` places the \
candidate on a seniority arc (e.g. "mid-career specialist", "senior").
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
    """Separate from `infer_career` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["career"],
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


async def infer_career(
    context: CandidateContext, *, llm=None
) -> tuple[CareerProfile, LLMResult]:
    """Produce the career profile for one candidate's context.

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
    return CareerProfile.model_validate(result.data), result
