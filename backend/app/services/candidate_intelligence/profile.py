"""Stage 3 — synthesise the professional profile.

The third and final stage. Fed both the career profile and the capability
profile, so it synthesises a professional identity and role affinities from the
evidence the earlier stages gathered, rather than re-reading the raw CV. This
is the candidate equivalent of the Job Intelligence "Person" view — but a
synthesis of *this* candidate's demonstrated work, not an ideal-person
inference.

Role affinity is a model hypothesis, not a factual statement about the
candidate. The prompt says so, and the UI presents the affinities as
suggestions (direct_fit / adjacent / transferable), not claims. This is the
design doc's (Phase 7) explicit guardrail.

Mirrors `job_intelligence/search.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.capability import model
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import capability_text, career_text
from app.services.candidate_intelligence.schema import (
    ProfessionalProfile,
    json_schema,
)
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment analyst. Given this candidate's CAREER and CAPABILITIES, \
synthesise the kind of professional they are and the roles they could fit.

Rules:
- `professional_identity` is a single phrase naming the professional this \
candidate is (e.g. "Insurance and financial services professional"). It is a \
synthesis of the career and capability evidence — never just the latest job \
title.
- `specializations` lists the areas the candidate has real depth in.
- `orientation` is a short phrase for how the candidate works (e.g. \
"analytical / operational / insurance technical").
- `role_affinity` is a list of objects. Each has:
  - `role`: the role title.
  - `affinity_type`: one of `direct_fit`, `adjacent`, or `transferable`.
    - `direct_fit` = the candidate's current title and work match this role.
    - `adjacent` = close but not exact; shared capabilities make it plausible.
    - `transferable` = different title, but the underlying capabilities transfer.
  - `confidence` (0.0–1.0): your honest estimate of how well the evidence \
supports this fit.
- Role affinity is a hypothesis, not a fact. Suggest roles the evidence \
supports; do not assert the candidate "is" a role they have not held unless it \
is a direct fit.
- Do not infer a career goal, industry preference, or relocation preference \
unless the record states it.
- Ignore any mention of the candidate's sex, race, nationality, age, religion \
or marital status.

Return JSON matching this schema:
{schema}

THE CAREER:
{career}

THE CAPABILITIES:
{capability}
"""


def build_prompt(
    career_as_text: str, capability_as_text: str
) -> str:
    """Separate from `infer_profile` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["profile"],
        career=career_as_text,
        capability=capability_as_text,
    )


async def infer_profile(
    context: CandidateContext, career, capability, *, llm=None
) -> tuple[ProfessionalProfile, LLMResult]:
    """Produce the professional profile for a candidate.

    `career` is the `CareerProfile` from Stage 1; `capability` the
    `CapabilityProfile` from Stage 2; `context` the candidate context. The
    context is threaded for signature symmetry with the other stages, though
    this stage reads only the upstream stage outputs.
    """
    resolve = llm or complete_json
    prompt = build_prompt(career_text(career), capability_text(capability))
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
    return ProfessionalProfile.model_validate(result.data), result
