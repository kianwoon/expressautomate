"""Module 2 — infer the ideal candidate persona for the work.

The second stage. Fed the work understanding from Module 1 (not the raw job
order) so the persona inherits the work's framing: the model reasons about the
*person who would do this work well*, not about keywords in a job ad.

Describes the ideal person for the role, never a specific candidate row. The
whole point is that the platform then goes looking for people who resemble
this persona — binding it to an existing candidate would defeat that.
"""

from app.core.config import settings
from app.services.job_intelligence.render import understanding_text
from app.services.job_intelligence.schema import CandidatePersona, json_schema
from app.services.job_intelligence.understand import model
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment analyst. Given this description of the WORK a role
involves, infer the kind of PERSON who would do it well.

Rules:
- Reason from the work, not from job titles in the ad. Transferable backgrounds
  matter more than literal matches: someone who has done adjacent work may fit
  better than someone with the exact title.
- `likely_backgrounds`, `transferable_roles`, `transferable_industries`,
  `behaviours` and `motivations` are lists of short phrases.
- `communication_style`, `career_stage`, `salary_expectation` and
  `availability` are single short phrases. Salary and availability should
  reflect what would attract this person to THIS role, grounded in the work
  and conditions described.
- Describe the ideal person for the work. Never name or assume a specific
  individual, and never infer a protected characteristic (race, nationality,
  gender, age, religion, marital status). If the work description contains
  "[redacted]", that is a withheld requirement — ignore it for the persona.

Return JSON matching this schema:
{schema}

THE WORK:
{understanding}

THE JOB ORDER (for context only):
{context}
"""


def build_prompt(context: str, understanding_as_text: str) -> str:
    """Separate from `infer_persona` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["persona"],
        understanding=understanding_as_text,
        context=context,
    )


async def infer_persona(
    context: str, understanding, *, llm=None
) -> tuple[CandidatePersona, LLMResult]:
    """Produce the ideal-person inference for a work understanding.

    `understanding` is a `JDUnderstanding`; `context` is the redacted
    opportunity text from `input.assemble`, passed for context only.
    """
    resolve = llm or complete_json
    prompt = build_prompt(context, understanding_text(understanding))
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
    return CandidatePersona.model_validate(result.data), result
