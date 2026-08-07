"""Pass 2 — automation/AI exposure + what remains human-scarce (doc L3+L4).

The second stage. Reads the decomposed history from Pass 1 and, for every
significant capability, assesses how much of the *work* (not the person) can
now be performed by software, workflow automation, rules engines, analytics,
generative AI, AI agents, AI-assisted systems, or self-service platforms
(design doc §6). Then it identifies what remains difficult to commoditize —
the residual human value (doc §7).

The critical principle: **assess the work, not the person** (doc §6, §17
guardrail 7). The engine evaluates the economic value of demonstrated
capabilities, not the worth of the human being.

Every assessment must carry both an `automation_reason` (why this level of
automation exposure) and a `residual_human_value` (what still requires the
human). Guardrails 5 and 6 forbid silent verdicts: if work is depreciated, the
reason travels with it; if a capability is scarce, the evidence shows why.

Domain knowledge alone is NOT automatically scarce (doc §7). The test is:
can the candidate produce an outcome that current AI/software cannot produce
sufficiently well?

Fed the history profile (not the raw CV) so the automation inference reasons
about the decomposed work rather than re-reading prose.

Mirrors `job_intelligence/persona.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.history import model
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import history_text
from app.services.candidate_intelligence.schema import (
    AutomationProfile,
    json_schema,
)
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a labour-market analyst assessing how much of this candidate's \
demonstrated work is now exposed to automation or AI, and what remains \
genuinely human and scarce.

You are assessing the WORK, not the person. The economic value of a piece of \
work is separate from the worth of the human being who performed it.

Rules:
- WEIGHT BY TENURE. The history notes each role's period and evidence. A \
capability drawn only from a short tenure (under ~12 months, or a role marked \
as onboarding exposure) is THIN — it means the candidate was exposed to the \
work, not that they built independent depth in it. Say so in the assessment: \
distinguish "demonstrated over years" from "exposed during a short stint". Do \
not treat a 6-month role's work the same as a 3-year role's work.
- `assessments` is a list of objects, one per significant capability or work \
area drawn from the candidate's decomposed history. For each:
  - `capability`: the capability or work area (e.g. "Commercial underwriting", \
"Data entry", "Risk assessment").
  - `automation_level`: how much of THIS work current software / AI / \
automation can perform. One of:
    - `very_high` — AI/software can perform most of the work
    - `high` — a large portion is automatable
    - `medium` — significant automation, but a meaningful human role remains
    - `low` — human judgment remains central
    - `very_low` — the work is highly dependent on scarce human capability
  - `automation_reason`: a candid sentence explaining WHY this automation \
level. Name the specific technology or operating-model shift where you can \
(e.g. "rules engines now apply standard underwriting decisions; LLMs draft \
the quotation prose"). Never leave this blank — a depreciation without a \
reason is a silent verdict.
  - `residual_human_value`: the part of this work that still requires a human \
— the scarce capability. If the work is fully automatable, state so plainly.
- `scarce_capabilities` is the cross-cutting list of capabilities this \
candidate holds that remain difficult to commoditize (doc §7): complex \
judgment, novel problem solving, bespoke design, high-consequence decisions, \
regulatory accountability, negotiation, relationship ownership, deep \
business-rule expertise, transformation, AI/system design, etc. Domain \
knowledge alone is NOT automatically scarce — only include a capability here \
if the candidate could produce an outcome current AI/software cannot.
- Do NOT assume every job will disappear because AI can perform some tasks \
(guardrail: no technology worship). Assess honestly: medium and low are valid \
answers, not failures.
- Do NOT infer capabilities the history does not support.
- Ignore any mention of the candidate's sex, race, nationality, age, religion \
or marital status.

Return JSON matching this schema:
{schema}

THE CANDIDATE'S HISTORY (roles + decomposed work):
{history}

CV (for additional context):
{cv}
"""


def build_prompt(context: CandidateContext, history_as_text: str) -> str:
    """Separate from `infer_automation` so a prompt change is testable without a model."""
    return PROMPT.format(
        schema=json_schema()["automation"],
        history=history_as_text,
        cv=context.cv_text or "(no CV text available)",
    )


async def infer_automation(
    context: CandidateContext, history, *, llm=None
) -> tuple[AutomationProfile, LLMResult]:
    """Produce the automation-exposure + human-scarcity profile.

    `history` is the `HistoryProfile` from Pass 1; `context` is the candidate
    context from `input.assemble`, passed so the model can read the CV for
    additional context.
    """
    resolve = llm or complete_json
    prompt = build_prompt(context, history_text(history))
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
    return AutomationProfile.model_validate(result.data), result
