"""Run all four Job Intelligence stages in sequence.

The orchestrator. Takes an `Opportunity` and its `OpportunityCode` rows,
assembles the redacted context once, and runs understand → classify_occupation
→ infer_persona → plan_search. Each stage feeds the next, so a change to the
understanding propagates through the occupation match, the persona, into the
search plan — but each stage remains unit-testable on its own, which is the
design doc's core principle.

The occupation stage is the only one that touches the database (it semantic-
searches the MOM reference library), so it takes an optional `session`; None
skips retrieval and yields no match — the rest of the analysis is still useful
without a salary benchmark.

Aggregates the token counts and latencies of all calls into one
`JobIntelligenceStats`, so the stored row records the full cost of an analysis
rather than only the last call's.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.job_intelligence.input import OpportunityContext, assemble
from app.services.job_intelligence.occupation import classify_occupation
from app.services.job_intelligence.persona import infer_persona
from app.services.job_intelligence.schema import JobIntelligenceResult
from app.services.job_intelligence.search import plan_search
from app.services.job_intelligence.understand import understand


@dataclass(frozen=True)
class JobIntelligenceStats:
    """The combined cost of the LLM calls in one analysis."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class JobIntelligenceOutcome:
    """The four-stage result plus its aggregate stats and redaction audit.

    `removed_codes` is forwarded from `assemble` so the API can record which
    protected-attribute codes were stripped before the model saw the job order.
    """

    result: JobIntelligenceResult
    stats: JobIntelligenceStats
    removed_codes: list[str]


async def analyze(
    opportunity,
    codes=(),
    *,
    session: AsyncSession | None = None,
    llm=None,
) -> JobIntelligenceOutcome:
    """Run the full Job Intelligence pipeline for one job order.

    `opportunity` is an `Opportunity`; `codes` its `OpportunityCode` rows.
    `session` is passed to the occupation stage for the MOM semantic search;
    None skips it. The `llm` seam is threaded through all stages so a test with
    one `FakeLLM` queued with responses runs the whole pipeline without a real
    call.
    """
    context: OpportunityContext = assemble(opportunity, codes)

    understanding, r1 = await understand(context.text, llm=llm)
    occupation, r_occ = await classify_occupation(
        context.text, understanding, session=session, llm=llm
    )
    persona, r2 = await infer_persona(context.text, understanding, llm=llm)
    search_plan, r3 = await plan_search(context.text, understanding, persona, llm=llm)

    stats = JobIntelligenceStats(
        model=r3.model,
        prompt_tokens=_sum(
            r1.prompt_tokens, r2.prompt_tokens, r3.prompt_tokens,
            r_occ.prompt_tokens if r_occ else 0,
        ),
        completion_tokens=_sum(
            r1.completion_tokens, r2.completion_tokens, r3.completion_tokens,
            r_occ.completion_tokens if r_occ else 0,
        ),
        latency_ms=_sum(
            r1.latency_ms, r2.latency_ms, r3.latency_ms,
            r_occ.latency_ms if r_occ else 0,
        ),
    )
    return JobIntelligenceOutcome(
        result=JobIntelligenceResult(
            understanding=understanding,
            persona=persona,
            search_plan=search_plan,
            occupation=occupation,
        ),
        stats=stats,
        removed_codes=context.removed_codes,
    )


def _sum(*values) -> int:
    """Add nullable token/latency counters, treating None as zero."""
    return sum(v or 0 for v in values)
