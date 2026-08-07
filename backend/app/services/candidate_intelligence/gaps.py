"""Pass 4 — capability gaps vs today's benchmark (doc L6 gap analysis).

The fourth stage. Joins the candidate's history + automation exposure against
the current market benchmark and identifies what is missing, weakly evidenced,
or contradicted. The output is the honest gap between the candidate and
today's standard — the thing a recruiter can act on (verify, develop, or
discount).

The load-bearing concept is the 5-way evidence distinction from design doc §2:

    demonstrated | partially_demonstrated | claimed_weak | not_evidenced |
    contradicted

"**Not evidenced != Does not possess**" (doc §2, guardrail 4). A capability
the CV does not mention is NOT proof the candidate lacks it — it is only proof
the CV did not evidence it. The prompt enforces this distinction: `not_evidenced`
means "the CV does not show it", never "the candidate cannot do it". This is
what prevents false negatives (guardrail 4).

Each gap carries a `note` explaining the status (guardrail 5/6 — no silent
verdict), and `evidence_gaps` lists the specific things a recruiter could
verify that would strengthen the assessment.

Fed history + automation + benchmark — all three upstream stages — because the
gap is measured against the benchmark's current requirements, informed by the
automation exposure of what the candidate already holds.

Mirrors `job_intelligence/persona.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.benchmark import model
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import (
    automation_text,
    benchmark_text,
    history_text,
)
from app.services.candidate_intelligence.schema import GapAnalysis, json_schema
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a recruitment analyst. Compare this candidate's demonstrated \
capabilities against TODAY's market benchmark for their work family, and \
identify the gaps.

The critical distinction: NOT EVIDENCED is NOT THE SAME AS DOES NOT POSSESS. \
A capability the CV does not mention is only proof the CV did not evidence it \
— never proof the candidate lacks it. Guard against false negatives.

Rules:
- `gaps` is a list of objects, one per capability that matters for the current \
benchmark (whether the candidate has it or not). For each:
  - `capability`: the capability being assessed.
  - `status`: one of:
    - `demonstrated` — the CV clearly shows this capability with action, \
responsibility, or repeated experience.
    - `partially_demonstrated` — the CV shows some evidence but it is thin, \
dated, or narrow in scope.
    - `claimed_weak` — the CV asserts the capability (e.g. "strong \
leadership") but the work behind it is not evidenced. Infer deep expertise \
only from action, not from adjectives or job titles.
    - `not_evidenced` — the CV does not show this capability. This is NOT a \
finding that the candidate lacks it — only that the CV did not evidence it. \
Say so plainly in the note.
    - `contradicted` — the CV's account is internally inconsistent or \
contradicts the claimed capability.
  - `note`: a candid sentence explaining the status. For `claimed_weak`, name \
what evidence is missing. For `not_evidenced`, state that absence of evidence \
is not evidence of absence. Never leave this blank.
- Do not infer capabilities the history does not support, and do not treat a \
missing capability as a weakness — mark it `not_evidenced` and move on.
- `evidence_gaps` is the list of specific things a recruiter could verify that \
would strengthen or change the assessment (e.g. "depth of business-rule \
ownership in reinsurance", "whether the candidate designed vs. only operated \
the administration platform").
- Ignore any mention of the candidate's sex, race, nationality, age, religion \
or marital status.

Return JSON matching this schema:
{schema}

THE CANDIDATE'S HISTORY:
{history}

AUTOMATION EXPOSURE:
{automation}

CURRENT MARKET BENCHMARK:
{benchmark}
"""


def build_prompt(
    context: CandidateContext,
    history_as_text: str,
    automation_as_text: str,
    benchmark_as_text: str,
) -> str:
    """Separate from `infer_gaps` so a prompt change is testable without a model.

    `context` is accepted for signature symmetry even though this prompt reads
    only the upstream stage outputs — the CV is already embodied in the history.
    """
    return PROMPT.format(
        schema=json_schema()["gaps"],
        history=history_as_text or "(none)",
        automation=automation_as_text or "(none)",
        benchmark=benchmark_as_text or "(none)",
    )


async def infer_gaps(
    context: CandidateContext,
    history,
    automation,
    benchmark,
    *,
    llm=None,
) -> tuple[GapAnalysis, LLMResult]:
    """Produce the gap analysis joining history, automation, and benchmark."""
    resolve = llm or complete_json
    prompt = build_prompt(
        context,
        history_text(history),
        automation_text(automation),
        benchmark_text(benchmark),
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
    return GapAnalysis.model_validate(result.data), result
