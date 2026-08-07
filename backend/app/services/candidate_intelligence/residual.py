"""Pass 5 — residual market value + candid current profile (doc L7 + L8).

The final stage, and the headline output. Reads all four upstream stages and
produces the decomposable residual-value assessment (doc §10) plus the candid
current-profile paragraph (doc §11).

Residual value is the amount of the candidate's demonstrated capability that
remains economically scarce and relevant under today's market conditions (doc
§10). It must NOT reduce to a single opaque score — it is a decomposable
assessment where every claim traces to evidence or a benchmark.

The central rule (doc §15):

    10 years experience != 10 years of current market value

A candidate with 15 years of experience in highly automated work may have
lower residual value than a candidate with 5 years in scarce, modern
capabilities. Years are input evidence, not the final answer.

The `current_profile` paragraph (doc §11) describes who the candidate is in
*TODAY's* market. Not "10+ years of insurance experience" — instead, a candid
sentence on what remains scarce and economically useful, what is exposed to
automation, and what the CV does not yet evidence. The profile must be candid
and evidence-based (doc §20): it is allowed to produce uncomfortable
conclusions.

The engine must NOT conclude "this candidate is low value" (doc §14). It
should conclude what is evidenced, what is exposed, and what needs
verification — then identify exactly what a recruiter could check.

Fed all four upstream stages: history (what they did), automation (what is
exposed), benchmark (what today requires), gaps (what is missing/not
evidenced).

Mirrors `job_intelligence/search.py` in shape (the last stage, fed all prior).
"""

from app.core.config import settings
from app.services.candidate_intelligence.gaps import model
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import (
    automation_text,
    benchmark_text,
    gaps_text,
    history_text,
)
from app.services.candidate_intelligence.schema import (
    ResidualValueAssessment,
    json_schema,
)
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a senior recruitment strategist. Synthesise everything below into \
a candid assessment of this candidate's RESIDUAL MARKET VALUE — the amount of \
their demonstrated capability that remains economically scarce and relevant \
under today's market conditions.

The central rule: YEARS OF EXPERIENCE ARE NOT YEARS OF CURRENT MARKET VALUE. \
A candidate with 15 years in highly automated work may have lower residual \
value than one with 5 years in scarce, modern capabilities. Re-price the \
historical experience against today's standard.

Rules:
- `historical_strength`: a candid phrase on the substance of the candidate's \
documented experience (what they genuinely did, at what depth).
- `automation_exposure`: a candid phrase on how much of their demonstrated \
work is now exposed to automation or AI.
- `current_relevance`: a candid phrase on how much of their experience still \
creates scarce economic value today. Do NOT apply a seniority premium — 10 \
years is not automatically better than 5.
- `scarce_capabilities`: the capabilities the candidate holds that remain \
difficult to commoditize and economically useful today.
- `depreciated_capabilities`: capabilities the candidate holds that have lost \
economic value (now largely automated or commoditized).
- `emerging_capabilities`: modern capabilities the candidate has begun to \
demonstrate (if any) — the appreciating skills.
- `evidence_gaps`: the specific things the CV does not yet evidence but a \
recruiter could verify, and that would change the assessment if confirmed.
- `overall_assessment`: a decomposable, candid summary — NOT a single opaque \
score. State plainly what is evidenced, what is exposed, and what the \
candidate's demonstrated residual value is under today's conditions.
- `current_profile`: a candid paragraph describing who this candidate is in \
TODAY's market. NOT "10+ years of X experience". Instead: what remains scarce \
and economically useful, what is exposed, and what the CV does not yet \
demonstrate enough of. The profile is allowed to produce uncomfortable \
conclusions — but it must be evidence-based, and it must NOT conclude "this \
candidate is low value" wholesale. It should conclude what is evidenced, what \
is exposed, and what needs verification.
- Do not make the candidate sound stronger than the evidence supports. Do not \
equate years of experience with value. Do not reward seniority automatically. \
Separate the economic value of the work from the worth of the person.

Return JSON matching this schema:
{schema}

THE CANDIDATE'S HISTORY:
{history}

AUTOMATION EXPOSURE:
{automation}

CURRENT MARKET BENCHMARK:
{benchmark}

GAP ANALYSIS:
{gaps}
"""


def build_prompt(
    context: CandidateContext,
    history_as_text: str,
    automation_as_text: str,
    benchmark_as_text: str,
    gaps_as_text: str,
) -> str:
    """Separate from `infer_residual` so a prompt change is testable without a model.

    `context` is accepted for signature symmetry even though this prompt reads
    only the upstream stage outputs — the CV is already embodied in the history.
    """
    return PROMPT.format(
        schema=json_schema()["residual"],
        history=history_as_text or "(none)",
        automation=automation_as_text or "(none)",
        benchmark=benchmark_as_text or "(none)",
        gaps=gaps_as_text or "(none)",
    )


async def infer_residual(
    context: CandidateContext,
    history,
    automation,
    benchmark,
    gaps,
    *,
    llm=None,
) -> tuple[ResidualValueAssessment, LLMResult]:
    """Produce the residual-value assessment + candid current profile."""
    resolve = llm or complete_json
    prompt = build_prompt(
        context,
        history_text(history),
        automation_text(automation),
        benchmark_text(benchmark),
        gaps_text(gaps),
    )
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
    return ResidualValueAssessment.model_validate(result.data), result
