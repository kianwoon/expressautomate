"""The orchestrator — all five stages in sequence, one fake model.

`analyze_candidate` threads one `llm` through all stages, so a single `FakeLLM`
queued with five responses runs the whole pipeline. This is the test that
keeps the stages honest as a pipeline: a change to the history must flow into
the automation and benchmark prompts, and from there into the gaps and
residual prompts.

The v2 pipeline (design doc §3):
    history → automation → benchmark → gaps → residual

Mirrors `test_job_intelligence_engine.py` in shape.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

from dataclasses import dataclass

import pytest

from app.core.config import settings
from app.services.candidate_intelligence.engine import analyze_candidate
from app.services.llm.client import FakeLLM


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@dataclass
class _Role:
    """The attributes `input._role_lines` reads, nothing more."""

    title: str = "Underwriter"
    employer: str = "Integral Plus"
    started_on: object = None
    ended_on: object = None
    description: str = ""
    status: str = "confirmed"


@dataclass
class _Candidate:
    """The attributes `input.assemble` reads, nothing more.

    Deliberately carries no protected-attribute fields (sex/race/DOB): the
    whitelist in `input.py` never reads them, and a test that included them
    would not prove they were excluded.
    """

    current_title: str = "Underwriter"
    current_employer: str = "Integral Plus"
    location: str = "Singapore"
    years_experience: int = 8
    expected_salary: object = None
    salary_currency: str = ""
    salary_period: str = ""
    employment_type: str = "Full-time"
    available_from: object = None
    notice_period_raw: str = ""


def _history_payload():
    return {
        "roles": [
            {
                "period": "2019–2023",
                "title": "Underwriter",
                "domain": "Insurance",
                "seniority": "mid",
                "scope": "Commercial book",
                "work": [
                    {
                        "task": "Assess commercial risk",
                        "tool": "Internal underwriting system",
                        "judgment_level": "high",
                        "accountability": "Underwriting decisions",
                    }
                ],
                "evidence": "Assessed companies' financial standing",
            }
        ],
        "industries": ["Insurance"],
        "functions": ["Underwriting"],
        "systems": ["Internal underwriting system"],
        "trajectory": ["Banking", "Insurance"],
    }


def _automation_payload():
    return {
        "assessments": [
            {
                "capability": "Commercial underwriting",
                "automation_level": "medium",
                "automation_reason": (
                    "Rules engines apply standard decisions; "
                    "human handles complex risk."
                ),
                "residual_human_value": "Complex or non-standard risk assessment",
            }
        ],
        "scarce_capabilities": ["Complex risk assessment"],
    }


def _benchmark_payload():
    return {
        "work_family": "Commercial insurance underwriting",
        "current_work": [
            "Rules-engine decisions for standard risk",
            "Human review for complex risk",
        ],
        "current_required": ["Risk assessment", "Business-rule design"],
        "declining": ["Manual data entry", "Routine quotation preparation"],
        "emerging": ["Automation design", "AI-assisted risk review"],
        "scarce": ["Complex risk judgment", "Business-rule design"],
        "automation_summary": (
            "Standard underwriting is largely rules-automated; "
            "complex risk remains human."
        ),
    }


def _gaps_payload():
    return {
        "gaps": [
            {
                "capability": "Business-rule design",
                "status": "not_evidenced",
                "note": (
                    "The CV shows operating the rules but not designing them. "
                    "Absence of evidence is not evidence of absence."
                ),
            }
        ],
        "evidence_gaps": [
            "Whether the candidate designed vs. only operated the underwriting rules"
        ],
    }


def _residual_payload():
    return {
        "historical_strength": "Solid commercial underwriting experience",
        "automation_exposure": "Standard underwriting is largely automated",
        "current_relevance": "Complex risk assessment remains valuable; routine work does not",
        "scarce_capabilities": ["Complex risk assessment"],
        "depreciated_capabilities": ["Routine quotation preparation"],
        "emerging_capabilities": [],
        "evidence_gaps": ["Depth of business-rule ownership"],
        "overall_assessment": (
            "Substantial history, but routine portions are automated; "
            "residual value lies in complex risk."
        ),
        "current_profile": (
            "An underwriter whose standard work is now largely automated; "
            "complex risk assessment is the residual value."
        ),
    }


async def test_analyze_runs_all_five_stages_in_sequence():
    llm = FakeLLM(
        _history_payload(),
        _automation_payload(),
        _benchmark_payload(),
        _gaps_payload(),
        _residual_payload(),
    )
    outcome = await analyze_candidate(
        _Candidate(),
        roles=[_Role()],
        skills=[],
        cv_text="A candidate with underwriting experience.",
        llm=llm,
    )

    assert outcome.result.history.roles[0].title == "Underwriter"
    assert outcome.result.automation.assessments[0].capability == "Commercial underwriting"
    assert outcome.result.benchmark.work_family == "Commercial insurance underwriting"
    assert outcome.result.gaps.gaps[0].capability == "Business-rule design"
    assert outcome.result.residual.overall_assessment.startswith("Substantial history")
    # Five calls: history, automation, benchmark, gaps, residual.
    assert len(llm.prompts) == 5


async def test_pipeline_carries_upstream_outputs_into_downstream_prompts():
    """The pipeline property: each stage's output flows into the next prompt.

    History's role title reaches the automation + benchmark prompts (which read
    the history); automation's capability reaches the gaps prompt; the gap's
    capability reaches the residual prompt. A change to any upstream stage must
    propagate, or the stages have come uncoupled.
    """
    llm = FakeLLM(
        _history_payload(),
        _automation_payload(),
        _benchmark_payload(),
        _gaps_payload(),
        _residual_payload(),
    )
    await analyze_candidate(_Candidate(), roles=[_Role()], llm=llm)

    # Pass 2 (automation) and Pass 3 (benchmark) read the history.
    assert "Underwriter" in llm.prompts[1]
    assert "Underwriter" in llm.prompts[2]
    # Pass 4 (gaps) reads history + automation + benchmark.
    assert "Underwriter" in llm.prompts[3]
    assert "Commercial underwriting" in llm.prompts[3]
    assert "Commercial insurance underwriting" in llm.prompts[3]
    # Pass 5 (residual) reads all four upstream stages.
    assert "Commercial underwriting" in llm.prompts[4]
    assert "Business-rule design" in llm.prompts[4]


async def test_analyze_aggregates_token_counts_across_five_stages():
    llm = FakeLLM(
        _history_payload(),
        _automation_payload(),
        _benchmark_payload(),
        _gaps_payload(),
        _residual_payload(),
    )
    outcome = await analyze_candidate(_Candidate(), llm=llm)
    # FakeLLM returns prompt_tokens=None; the engine treats None as zero, so the
    # aggregate is 0 rather than a TypeError on None + None.
    assert outcome.stats.prompt_tokens == 0
    assert outcome.stats.completion_tokens == 0
    assert outcome.stats.latency_ms == 0


async def test_analyze_carries_cv_text_into_history_prompt():
    """The CV text is the primary source the history stage reads."""
    llm = FakeLLM(
        _history_payload(),
        _automation_payload(),
        _benchmark_payload(),
        _gaps_payload(),
        _residual_payload(),
    )
    await analyze_candidate(_Candidate(), cv_text="UNIQUE CV MARKER TEXT", llm=llm)
    assert "UNIQUE CV MARKER TEXT" in llm.prompts[0]


async def test_not_evidenced_is_not_does_not_possess():
    """Guardrail 4: the gap stage must never treat 'not evidenced' as a deficit.

    The gap fixture marks 'Business-rule design' as not_evidenced; the note
    must carry the 'absence of evidence is not evidence of absence' wording so
    a reader cannot mistake it for a finding that the candidate lacks the
    capability. This is the load-bearing distinction from design doc §2.
    """
    llm = FakeLLM(
        _history_payload(),
        _automation_payload(),
        _benchmark_payload(),
        _gaps_payload(),
        _residual_payload(),
    )
    outcome = await analyze_candidate(_Candidate(), roles=[_Role()], llm=llm)
    gap = outcome.result.gaps.gaps[0]
    assert gap.status == "not_evidenced"
    assert "not evidence of absence" in gap.note.lower()


async def test_no_seniority_premium_in_residual():
    """Guardrail 1: residual current_relevance must not apply a seniority premium.

    The candidate has 8 years of experience, but the residual assessment must
    not read 'senior' or reward the years themselves — it must reason about the
    *work's* current value. The fixture's current_relevance names the work
    ('complex risk assessment remains valuable'), not the years.
    """
    llm = FakeLLM(
        _history_payload(),
        _automation_payload(),
        _benchmark_payload(),
        _gaps_payload(),
        _residual_payload(),
    )
    outcome = await analyze_candidate(_Candidate(), roles=[_Role()], llm=llm)
    relevance = outcome.result.residual.current_relevance.lower()
    assert "complex risk" in relevance
    # The residual must not simply echo the years of experience as value.
    assert "8 years" not in outcome.result.residual.current_relevance
