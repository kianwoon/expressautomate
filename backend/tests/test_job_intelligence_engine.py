"""The orchestrator — all three stages in sequence, one fake model.

`analyze` threads one `llm` through all three stages, so a single `FakeLLM`
queued with three responses runs the whole pipeline. This is the test that
keeps the stages honest as a pipeline: a change to the understanding must flow
into the persona prompt, and from there into the search prompt.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

from dataclasses import dataclass

import pytest

from app.core.config import settings
from app.services.job_intelligence.engine import analyze
from app.services.llm.client import FakeLLM


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@dataclass
class _Opp:
    """The attributes `input.assemble` reads, nothing more."""

    job_title_raw: str = "Logistics Manager"
    job_description: str = "Run the warehouse day shift."
    requirements: str = "3+ years in logistics."
    working_hours_raw: str = ""
    salary_raw: str = ""
    duration_raw: str = ""
    location_raw: str = "Singapore"
    salary_min: object = None
    salary_max: object = None
    salary_currency: str = ""
    salary_period: str = "month"
    duration_months: object = None
    employment_type: str = "Full-time"
    work_arrangement: str = "onsite"
    industry: str = ""
    skills: list = None


def _understanding_payload():
    return {
        "role": "Logistics Manager",
        "business_purpose": "Keep the warehouse running",
        "daily_activities": ["Plan dispatch"],
        "work_environment": "Warehouse",
        "must_have_requirements": ["3 years logistics"],
        "preferred_requirements": [],
        "working_conditions": "Onsite",
        "success_characteristics": [],
        "potential_challenges": [],
        "confidence": 0.8,
    }


def _persona_payload():
    return {
        "likely_backgrounds": ["Logistics coordinator"],
        "transferable_roles": [],
        "transferable_industries": [],
        "behaviours": [],
        "communication_style": "Direct",
        "career_stage": "Mid-level",
        "motivations": [],
        "salary_expectation": "SGD 4,500/month",
        "availability": "1 month",
    }


def _search_payload():
    return {
        "platform": "LinkedIn",
        "priority": 2,
        "queries": ['"logistics manager" AND dispatch'],
        "negative_queries": ["intern"],
        "salary": "SGD 4,500/month",
        "location": "Singapore",
        "employment_type": "Full-time",
    }


async def test_analyze_runs_all_three_stages_in_sequence():
    llm = FakeLLM(_understanding_payload(), _persona_payload(), _search_payload())
    outcome = await analyze(_Opp(), codes=(), llm=llm)

    assert outcome.result.understanding.role == "Logistics Manager"
    assert outcome.result.persona.likely_backgrounds == ["Logistics coordinator"]
    assert outcome.result.search_plan.platform == "LinkedIn"
    # Three calls, one per stage.
    assert len(llm.prompts) == 3
    # The understanding's role flowed into the persona prompt, and the persona's
    # background into the search prompt — the pipeline property.
    assert "Logistics Manager" in llm.prompts[1]
    assert "Logistics coordinator" in llm.prompts[2]


async def test_analyze_aggregates_token_counts_across_stages():
    llm = FakeLLM(_understanding_payload(), _persona_payload(), _search_payload())
    outcome = await analyze(_Opp(), codes=(), llm=llm)
    # FakeLLM returns prompt_tokens=None; the engine treats None as zero, so the
    # aggregate is 0 rather than a TypeError on None + None.
    assert outcome.stats.prompt_tokens == 0
    assert outcome.stats.completion_tokens == 0
    assert outcome.stats.latency_ms == 0


async def test_analyze_records_removed_codes():
    """The redaction audit flows out of the orchestrator."""
    llm = FakeLLM(_understanding_payload(), _persona_payload(), _search_payload())
    outcome = await analyze(_Opp(), codes=(), llm=llm)
    # No codes in this fixture, so nothing was removed.
    assert outcome.removed_codes == []
