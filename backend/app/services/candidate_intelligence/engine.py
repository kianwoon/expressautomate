"""Run all five Candidate Intelligence v2 stages in sequence.

The orchestrator. Takes a `Candidate`, its `CandidateRole`/`CandidateSkill`
rows, and the parsed CV text, assembles the context once, and runs the
five-stage residual-value pipeline:

    history → automation → benchmark → gaps → residual

Each stage feeds the next, so a change to the history propagates through the
automation and benchmark into the gaps and the final residual assessment — but
each stage remains unit-testable on its own, which is the design doc's core
principle.

The pipeline structure mirrors the design doc's conceptual model (§3):
  Pass 1 (history) = Layer 1 facts + Layer 2 work decomposition
  Pass 2 (automation) = Layer 3 automation test + Layer 4 human scarcity
  Pass 3 (benchmark) = Layer 5 current benchmark + Layer 6 depreciation
  Pass 4 (gaps) = Layer 6 gap analysis
  Pass 5 (residual) = Layer 7 residual value + Layer 8 current profile

No stage touches the database (the CV text is fetched in the worker before
`analyze_candidate` is called), so the orchestrator takes no session — unlike
`job_intelligence/engine.py`, whose occupation stage semantic-searches the MOM
library. Aggregates the token counts and latencies of all five calls into one
`CandidateIntelligenceStats`, so the stored row records the full cost of an
analysis rather than only the last call's.

Mirrors `job_intelligence/engine.py` in shape.
"""

from dataclasses import dataclass

from app.services.candidate_intelligence.automation import infer_automation
from app.services.candidate_intelligence.benchmark import infer_benchmark
from app.services.candidate_intelligence.gaps import infer_gaps
from app.services.candidate_intelligence.history import infer_history
from app.services.candidate_intelligence.input import CandidateContext, assemble
from app.services.candidate_intelligence.residual import infer_residual
from app.services.candidate_intelligence.schema import CandidateIntelligenceResult


@dataclass(frozen=True)
class CandidateIntelligenceStats:
    """The combined cost of the LLM calls in one analysis."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class CandidateIntelligenceOutcome:
    """The five-stage result plus its aggregate stats."""

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
    """Run the full Candidate Intelligence v2 pipeline for one candidate.

    `candidate` is a `Candidate`; `roles` its `CandidateRole` rows; `skills` its
    `CandidateSkill` rows; `cv_text` the parsed CV fetched from R2. The `llm`
    seam is threaded through all stages so a test with one `FakeLLM` queued with
    responses runs the whole pipeline without a real call.
    """
    context: CandidateContext = assemble(candidate, roles, skills, cv_text)

    history, r1 = await infer_history(context, llm=llm)
    automation, r2 = await infer_automation(context, history, llm=llm)
    benchmark, r3 = await infer_benchmark(context, history, llm=llm)
    gaps, r4 = await infer_gaps(context, history, automation, benchmark, llm=llm)
    residual, r5 = await infer_residual(
        context, history, automation, benchmark, gaps, llm=llm
    )

    stats = CandidateIntelligenceStats(
        model=r5.model,
        prompt_tokens=_sum(
            r1.prompt_tokens,
            r2.prompt_tokens,
            r3.prompt_tokens,
            r4.prompt_tokens,
            r5.prompt_tokens,
        ),
        completion_tokens=_sum(
            r1.completion_tokens,
            r2.completion_tokens,
            r3.completion_tokens,
            r4.completion_tokens,
            r5.completion_tokens,
        ),
        latency_ms=_sum(
            r1.latency_ms,
            r2.latency_ms,
            r3.latency_ms,
            r4.latency_ms,
            r5.latency_ms,
        ),
    )
    return CandidateIntelligenceOutcome(
        result=CandidateIntelligenceResult(
            history=history,
            automation=automation,
            benchmark=benchmark,
            gaps=gaps,
            residual=residual,
        ),
        stats=stats,
    )


def _sum(*values) -> int:
    """Add nullable token/latency counters, treating None as zero."""
    return sum(v or 0 for v in values)
