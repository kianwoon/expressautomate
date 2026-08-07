"""The orchestrator — all three stages in sequence, one fake model.

`analyze_candidate` threads one `llm` through all stages, so a single `FakeLLM`
queued with three responses runs the whole pipeline. This is the test that
keeps the stages honest as a pipeline: a change to the career must flow into the
capability prompt, and from there into the profile prompt.

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


def _career_payload():
    return {
        "timeline": [
            {"period": "2019–2023", "title": "Underwriter", "domain": "Insurance"},
        ],
        "trajectory": ["Banking", "Insurance"],
        "primary_domain": "Insurance",
        "secondary_domains": ["Banking"],
        "career_direction": "Deepening specialisation in insurance",
        "career_stage": "Mid-career specialist",
    }


def _capability_payload():
    return {
        "capabilities": [
            {
                "capability": "Commercial underwriting",
                "category": "functional",
                "confidence": 0.9,
                "supporting_evidence": "Assessing companies' financial standing",
            },
        ],
        "tools": ["Microsoft Excel"],
    }


def _profile_payload():
    return {
        "professional_identity": "Insurance and financial services professional",
        "specializations": ["Underwriting"],
        "orientation": "Analytical",
        "role_affinity": [
            {"role": "Underwriter", "affinity_type": "direct_fit", "confidence": 0.95},
        ],
    }


async def test_analyze_runs_all_stages_in_sequence():
    llm = FakeLLM(_career_payload(), _capability_payload(), _profile_payload())
    outcome = await analyze_candidate(
        _Candidate(),
        roles=[_Role()],
        skills=[],
        cv_text="A candidate with underwriting experience.",
        llm=llm,
    )

    assert outcome.result.career.primary_domain == "Insurance"
    assert outcome.result.capability.capabilities[0].capability == "Commercial underwriting"
    assert outcome.result.profile.professional_identity == (
        "Insurance and financial services professional"
    )
    # Three calls: career, capability, profile.
    assert len(llm.prompts) == 3
    # The career's primary domain flowed into the capability prompt, and the
    # capability's first entry into the profile prompt — the pipeline property.
    assert "Insurance" in llm.prompts[1]
    assert "Commercial underwriting" in llm.prompts[2]


async def test_analyze_aggregates_token_counts_across_stages():
    llm = FakeLLM(_career_payload(), _capability_payload(), _profile_payload())
    outcome = await analyze_candidate(_Candidate(), llm=llm)
    # FakeLLM returns prompt_tokens=None; the engine treats None as zero, so the
    # aggregate is 0 rather than a TypeError on None + None.
    assert outcome.stats.prompt_tokens == 0
    assert outcome.stats.completion_tokens == 0
    assert outcome.stats.latency_ms == 0


async def test_analyze_carries_cv_text_into_career_prompt():
    """The CV text is the primary source the career stage reads."""
    llm = FakeLLM(_career_payload(), _capability_payload(), _profile_payload())
    await analyze_candidate(_Candidate(), cv_text="UNIQUE CV MARKER TEXT", llm=llm)
    assert "UNIQUE CV MARKER TEXT" in llm.prompts[0]
