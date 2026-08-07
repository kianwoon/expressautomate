"""Run all three Candidate Intelligence stages in sequence.

The orchestrator. Takes a `Candidate`, its `CandidateRole`/`CandidateSkill`
rows, and the parsed CV text, assembles the context once, and runs
career → capability → profile. Each stage feeds the next, so a change to the
career propagates through the capabilities into the profile — but each stage
remains unit-testable on its own, which is the design doc's core principle.

No stage touches the database (the CV text is fetched in the worker before
`analyze_candidate` is called), so the orchestrator takes no session — unlike
`job_intelligence/engine.py`, whose occupation stage semantic-searches the MOM
library. Aggregates the token counts and latencies of all three calls into one
`CandidateIntelligenceStats`, so the stored row records the full cost of an
analysis rather than only the last call's.

Mirrors `job_intelligence/engine.py` in shape.
"""

from dataclasses import dataclass

from app.services.candidate_intelligence.capability import infer_capability
from app.services.candidate_intelligence.career import infer_career
from app.services.candidate_intelligence.input import CandidateContext, assemble
from app.services.candidate_intelligence.profile import infer_profile
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
    """The three-stage result plus its aggregate stats."""

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
    """Run the full Candidate Intelligence pipeline for one candidate.

    `candidate` is a `Candidate`; `roles` its `CandidateRole` rows; `skills` its
    `CandidateSkill` rows; `cv_text` the parsed CV fetched from R2. The `llm`
    seam is threaded through all stages so a test with one `FakeLLM` queued with
    responses runs the whole pipeline without a real call.
    """
    context: CandidateContext = assemble(candidate, roles, skills, cv_text)

    career, r1 = await infer_career(context, llm=llm)
    capability, r2 = await infer_capability(context, career, llm=llm)
    profile, r3 = await infer_profile(context, career, capability, llm=llm)

    stats = CandidateIntelligenceStats(
        model=r3.model,
        prompt_tokens=_sum(r1.prompt_tokens, r2.prompt_tokens, r3.prompt_tokens),
        completion_tokens=_sum(
            r1.completion_tokens, r2.completion_tokens, r3.completion_tokens
        ),
        latency_ms=_sum(r1.latency_ms, r2.latency_ms, r3.latency_ms),
    )
    return CandidateIntelligenceOutcome(
        result=CandidateIntelligenceResult(
            career=career,
            capability=capability,
            profile=profile,
        ),
        stats=stats,
    )


def _sum(*values) -> int:
    """Add nullable token/latency counters, treating None as zero."""
    return sum(v or 0 for v in values)
