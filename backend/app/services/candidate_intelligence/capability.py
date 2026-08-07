"""Stage 2 — extract the capabilities the career demonstrates.

The second stage. Fed the career profile from Stage 1 (plus the CV for the
supporting evidence) so the capability inference reasons about demonstrated work
rather than re-reading the raw CV. Capabilities are grouped by category
(domain / functional / operational), and each carries a confidence and a short
verbatim quote from the CV as provenance.

A capability should be supported by an action, responsibility, or repeated
experience — the prompt enforces this, so the model does not treat every noun
in a CV as a skill. This is the design doc's (Phase 4) core principle: a CV
saying "strong leadership" does not automatically become `leadership = expert`.

Mirrors `job_intelligence/persona.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.career import model
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import career_text
from app.services.candidate_intelligence.schema import (
    CapabilityProfile,
    json_schema,
)
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment analyst. Given this candidate's CAREER, identify the \
CAPABILITIES they have actually demonstrated.

Rules:
- Reason from responsibilities, scope, and outcomes — not from job titles or \
self-description. "Strong leadership" in a CV does not by itself prove a \
leadership capability; the work behind it does.
- `capabilities` is a list of objects. Each has:
  - `capability`: the skill or area of ability (e.g. "Commercial underwriting").
  - `category`: one of `domain`, `functional`, or `operational`.
    - `domain` = an industry or knowledge area (Insurance, Reinsurance, Banking).
    - `functional` = a type of work (Underwriting, Financial analysis, Reporting).
    - `operational` = a way of working (Stakeholder coordination, Process \
compliance).
  - `confidence` (0.0–1.0): your honest estimate of how well the CV supports \
this capability, derived from directness, recency, and duration.
  - `supporting_evidence`: a short verbatim phrase quoted from the CV that \
demonstrates the capability. If you cannot find one, lower the confidence.
- `tools` is a list of named tools or systems the candidate used (e.g. \
"SAP", "Microsoft Excel"). Only include tools the CV actually mentions.
- Do not infer capabilities the record does not support, and do not treat a \
missing capability as a weakness — leave it out rather than guessing.
- Ignore any mention of the candidate's sex, race, nationality, age, religion \
or marital status.

Return JSON matching this schema:
{schema}

THE CAREER:
{career}

CV (for supporting evidence):
{cv}
"""


def build_prompt(context: CandidateContext, career_as_text: str) -> str:
    """Separate from `infer_capability` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["capability"],
        career=career_as_text,
        cv=context.cv_text or "(no CV text available)",
    )


async def infer_capability(
    context: CandidateContext, career, *, llm=None
) -> tuple[CapabilityProfile, LLMResult]:
    """Produce the evidence-backed capability profile for a candidate.

    `career` is the `CareerProfile` from Stage 1; `context` is the candidate
    context from `input.assemble`, passed so the model can quote the CV.
    """
    resolve = llm or complete_json
    prompt = build_prompt(context, career_text(career))
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
    return CapabilityProfile.model_validate(result.data), result
