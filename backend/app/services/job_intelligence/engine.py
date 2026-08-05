"""Run all three Job Intelligence stages in sequence.

The orchestrator. Takes an `Opportunity` and its `OpportunityCode` rows,
assembles the redacted context once, and runs understand → infer_persona →
plan_search. Each stage feeds the next, so a change to the understanding
propagates through the persona into the search plan — but each stage remains
unit-testable on its own, which is the design doc's core principle.

Aggregates the token counts and latencies of all three calls into one
`JobIntelligenceStats`, so the stored row records the full cost of an analysis
rather than only the last call's.
"""

from dataclasses import dataclass

from app.services.job_intelligence.input import OpportunityContext, assemble
from app.services.job_intelligence.persona import infer_persona
from app.services.job_intelligence.schema import JobIntelligenceResult
from app.services.job_intelligence.search import plan_search
from app.services.job_intelligence.understand import understand


@dataclass(frozen=True)
class JobIntelligenceStats:
    """The combined cost of the three LLM calls in one analysis."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class JobIntelligenceOutcome:
    """The three-stage result plus its aggregate stats and redaction audit.

    `removed_codes` is forwarded from `assemble` so the API can record which
    protected-attribute codes were stripped before the model saw the job order.
    """

    result: JobIntelligenceResult
    stats: JobIntelligenceStats
    removed_codes: list[str]


async def analyze(opportunity, codes=(), *, llm=None) -> JobIntelligenceOutcome:
    """Run the full Job Intelligence pipeline for one job order.

    `opportunity` is an `Opportunity`; `codes` its `OpportunityCode` rows. The
    `llm` seam is threaded through all three stages so a test with one `FakeLLM`
    queued with three responses runs the whole pipeline without a real call.
    """
    context: OpportunityContext = assemble(opportunity, codes)

    understanding, r1 = await understand(context.text, llm=llm)
    persona, r2 = await infer_persona(context.text, understanding, llm=llm)
    search_plan, r3 = await plan_search(context.text, understanding, persona, llm=llm)

    stats = JobIntelligenceStats(
        model=r3.model,
        prompt_tokens=_sum(r1.prompt_tokens, r2.prompt_tokens, r3.prompt_tokens),
        completion_tokens=_sum(
            r1.completion_tokens, r2.completion_tokens, r3.completion_tokens
        ),
        latency_ms=_sum(r1.latency_ms, r2.latency_ms, r3.latency_ms),
    )
    return JobIntelligenceOutcome(
        result=JobIntelligenceResult(
            understanding=understanding,
            persona=persona,
            search_plan=search_plan,
        ),
        stats=stats,
        removed_codes=context.removed_codes,
    )


def _sum(*values) -> int:
    """Add nullable token/latency counters, treating None as zero."""
    return sum(v or 0 for v in values)
