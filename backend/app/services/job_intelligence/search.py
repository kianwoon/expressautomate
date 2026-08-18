"""Module 3 — generate a search strategy for the ideal person.

The third stage. Fed both the work understanding and the persona, so its
queries aim at the people Module 2 described and respect the conditions Module
1 surfaced. The platform-specific drivers (FastJobs, MyCareersFuture) are
Phase 2; this is the strategy those drivers will consume.

`queries` and `negative_queries` are boolean-style search strings. They are the
artifact a platform driver translates into a real query, so they are kept as
plain text a recruiter can also read and edit.
"""

from app.core.config import settings
from app.services.job_intelligence.render import persona_text, understanding_text
from app.services.job_intelligence.schema import SearchPlan, json_schema
from app.services.job_intelligence.understand import model
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment sourcing strategist. Given the work and the ideal
candidate for it, write a search strategy a recruiter can act on.

Rules:
- `queries` is a list of boolean-style search strings (e.g.
  '("operations manager" OR "ops executive") AND logistics'). Each one is a
  distinct angle worth trying, ordered most-promising first.
- `negative_queries` is a list of terms to exclude (e.g. 'intern', 'student').
- `platform` names where this persona is most findable for this role
  (e.g. 'LinkedIn', 'MyCareersFuture', 'FastJobs'). One platform per plan.
- `priority` is 1 (highest) to 5, your honest ranking of how fruitful this
  platform is likely to be for this persona.
- `salary`, `location` and `employment_type` are the constraints to apply, each
  a short phrase, grounded in the job order and persona.
- Never include or exclude on a protected characteristic (race, nationality,
  gender, age, religion, marital status). If the work or job order contains
  "[redacted]", ignore it for the strategy.

Return JSON matching this schema:
{schema}

THE WORK:
{understanding}

THE IDEAL PERSON:
{persona}

THE JOB ORDER (for context only):
{context}
"""


def build_prompt(
    context: str, understanding_as_text: str, persona_as_text: str
) -> str:
    """Separate from `plan_search` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["search"],
        understanding=understanding_as_text,
        persona=persona_as_text,
        context=context,
    )


async def plan_search(
    context: str, understanding, persona, *, llm=None
) -> tuple[SearchPlan, LLMResult]:
    """Produce the search strategy for a work understanding and persona.

    `understanding` is a `JDUnderstanding`, `persona` a `CandidatePersona`, and
    `context` the redacted opportunity text from `input.assemble`.
    """
    resolve = llm or complete_json
    prompt = build_prompt(
        context,
        understanding_text(understanding),
        persona_text(persona),
    )
    result = await resolve(
        prompt,
        model=model(),
        schema=None,
        base_url=settings.LLM_PROVIDER_BASE_URL,
        api_key=settings.LLM_PROVIDER_API_KEY,
        extra_body={
            "max_tokens": settings.JOB_INTELLIGENCE_MAX_TOKENS,
            "reasoning_effort": settings.JOB_INTELLIGENCE_REASONING_EFFORT,
        },
    )
    return SearchPlan.model_validate(result.data), result
