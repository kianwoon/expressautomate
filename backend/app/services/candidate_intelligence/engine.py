"""Run the 2-stage Candidate Intelligence v2 pipeline.

    work → assessment

Pass 1 (work) decomposes every role into work units with decision ownership,
AI heavy-lift, claim-vs-substance inflation detection, and evidence levels.
Pass 2 (assessment) synthesises that into a blunt, sharp read.

Two deep passes, not five shallow ones. Fewer handoffs, more reasoning per
call. The work pass is the foundation — if it's shallow, the assessment
inherits the shallowness.

No stage touches the database (CV text is fetched in the worker before
`analyze_candidate` is called). Aggregates token counts and latencies into one
`CandidateIntelligenceStats`.
"""

from dataclasses import dataclass

from app.services.candidate_intelligence.assessment import infer_assessment
from app.services.candidate_intelligence.input import CandidateContext, assemble
from app.services.candidate_intelligence.schema import CandidateIntelligenceResult
from app.services.candidate_intelligence.work import infer_work


@dataclass(frozen=True)
class CandidateIntelligenceStats:
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class CandidateIntelligenceOutcome:
    result: CandidateIntelligenceResult
    stats: CandidateIntelligenceStats


async def analyze_candidate(
    candidate,
    roles=(),
    skills=(),
    cv_text: str = "",
    *,
    llm=None,
) -> CandidateIntelligenceOutcome:
    context: CandidateContext = assemble(candidate, roles, skills, cv_text)

    work, r1 = await infer_work(context, llm=llm)
    assessment, r2 = await infer_assessment(context, work, llm=llm)

    stats = CandidateIntelligenceStats(
        model=r2.model,
        prompt_tokens=_sum(r1.prompt_tokens, r2.prompt_tokens),
        completion_tokens=_sum(r1.completion_tokens, r2.completion_tokens),
        latency_ms=_sum(r1.latency_ms, r2.latency_ms),
    )
    return CandidateIntelligenceOutcome(
        result=CandidateIntelligenceResult(
            work=work,
            assessment=assessment,
        ),
        stats=stats,
    )


def _sum(*values) -> int:
    return sum(v or 0 for v in values)
