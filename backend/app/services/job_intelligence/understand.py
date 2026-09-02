"""Module 1 — understand what work the job order describes.

The first stage of the engine. Reads the redacted opportunity context and
answers: what is the role, what is it for, what does the person do day to day,
and what does success look like. Its output feeds the persona stage, so the
ideal-person inference inherits the work's own framing rather than re-reading
the raw job order.

Single LLM pass, not the two-pass escalation `ingest/extract.py` uses. Escalation
there exists because extraction has a deterministic check it can fail
(`quality_state` — a quote the email does not contain). Understanding is an
interpretation with no quote to verify, so there is no signal that would justify
a second, more expensive call: paying twice for the same question at temperature
zero buys the same answer twice.
"""

from app.core.config import settings
from app.services.job_intelligence.schema import JDUnderstanding, json_schema
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
#
# The grounding rule is two-tiered on purpose. Order-specific facts
# (requirements, conditions, salary) must come from the order — the
# anti-fabrication rule (§15). But `role`, `business_purpose` and
# `daily_activities` are allowed to draw on what the title denotes in the
# industry, phrased as typical. The thin-order guard already made that trade
# explicit: a recruiter who presses "Run anyway" on a title-only order is
# asking exactly for the typical meaning of the title, and a prompt that
# answers "duties not described" makes the escape hatch useless. The
# confidence field keeps the honesty: it scores the ORDER, not the write-up.
PROMPT = """You are a recruitment analyst. Read this job order and explain the WORK it
describes — not the keywords, the work. Reason about what the person will
actually do, why the role exists, and what doing it well looks like.

Rules:
- Never refuse and never answer in prose: always return valid JSON.
- Separate what the ORDER states from what the TITLE implies. Requirements,
  salary and working conditions must come from the order itself; where the
  order is silent, say so plainly ("not stated in the order") rather than
  inventing specifics.
- `role`, `business_purpose` and `daily_activities` may draw on what this
  title denotes in the industry: when the order gives little detail, describe
  the work such a role typically covers. Phrase typical knowledge as typical
  ("typically covers…", "generally exists to…") — never present it as
  something the order stated. `work_environment` too when the order is silent
  (e.g. "typically office-based; the order does not specify").
- If a field below is absent or says "[redacted]", treat it as unknown and say
  so in plain words rather than guessing. "[redacted]" marks a requirement that
  was withheld for legal reasons — never treat it as a skill or a preference.
- `daily_activities`, `must_have_requirements`, `preferred_requirements`,
  `success_characteristics` and `potential_challenges` are lists of short
  phrases. `role`, `business_purpose`, `work_environment` and
  `working_conditions` are single sentences.
- `confidence` (0.0–1.0) is your honest estimate of how complete and
  unambiguous the JOB ORDER was — not how full your write-up is. A title-only
  order stays near 0.0–0.3 even when the typical-role description is rich.

Return JSON matching this schema:
{schema}

JOB ORDER:
{context}
"""


def build_prompt(context: str) -> str:
    """Separate from `understand` so a prompt change is testable without a model."""
    return PROMPT.format(schema=json_schema()["understanding"], context=context)


def model() -> str:
    """The model to ask, defaulting to the fast extraction model.

    An empty `JOB_INTELLIGENCE_MODEL` falls back to `EXTRACTION_MODEL_FAST`
    rather than being required, so a deployment that names only the one model
    still runs — the same fallback idiom `EXTRACTION_MODEL_STRONG` uses.

    When the Job Intelligence LLM provider is enabled
    (`JOB_INTELLIGENCE_LLM_ENABLED` + an API key), the engine asks the GLM
    model name instead; the model id lives in `JOB_INTELLIGENCE_LLM_MODEL_NAME`.
    """
    if settings.JOB_INTELLIGENCE_LLM_ENABLED and settings.JOB_INTELLIGENCE_LLM_API_KEY:
        return settings.JOB_INTELLIGENCE_LLM_MODEL_NAME or "glm-5.3"
    return settings.JOB_INTELLIGENCE_MODEL or settings.EXTRACTION_MODEL_FAST


async def understand(context: str, *, llm=None) -> tuple[JDUnderstanding, LLMResult]:
    """Produce the work understanding for one job order's context.

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
        base_url=settings.LLM_PROVIDER_BASE_URL,
        api_key=settings.LLM_PROVIDER_API_KEY,
        extra_body={
            "max_tokens": settings.JOB_INTELLIGENCE_MAX_TOKENS,
            "reasoning_effort": settings.JOB_INTELLIGENCE_REASONING_EFFORT,
        },
    )
    return JDUnderstanding.model_validate(result.data), result
