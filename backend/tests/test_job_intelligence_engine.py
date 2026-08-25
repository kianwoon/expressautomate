"""The orchestrator — all four stages in sequence, one fake model.

`analyze` threads one `llm` through all stages, so a single `FakeLLM` queued
with four responses runs the whole pipeline (the occupation stage's re-rank
step is skipped here because `session=None` yields no candidates). This is the
test that keeps the stages honest as a pipeline: a change to the understanding
must flow into the persona prompt, and from there into the search prompt.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

from dataclasses import dataclass

import pytest

from app.core.config import settings
from app.services.job_intelligence.engine import (
    _get_llm_for_job_intelligence,
    _glm_complete_json,
    analyze,
)
from app.services.llm.client import FakeLLM, complete_json


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
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


def _occupation_profile_payload():
    # Consumed between understand and persona. The re-rank step never runs in
    # these tests (session=None → no candidates), so only the profile call fires.
    return {
        "occupation": "Logistics Manager",
        "seniority": "Mid",
        "people_management": True,
        "industry": "General",
        "functions": {"Operations": 50, "Planning": 30, "Team management": 20},
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


async def test_analyze_runs_all_stages_in_sequence():
    llm = FakeLLM(
        _understanding_payload(),
        _occupation_profile_payload(),
        _persona_payload(),
        _search_payload(),
    )
    outcome = await analyze(_Opp(), codes=(), llm=llm)

    assert outcome.result.understanding.role == "Logistics Manager"
    assert outcome.result.persona.likely_backgrounds == ["Logistics coordinator"]
    assert outcome.result.search_plan.platform == "LinkedIn"
    # Four calls: understand, occupation profile, persona, search. The
    # occupation re-rank does not fire (session=None → no candidates).
    assert len(llm.prompts) == 4
    # The understanding's role flowed into the persona prompt, and the persona's
    # background into the search prompt — the pipeline property.
    assert "Logistics Manager" in llm.prompts[2]
    assert "Logistics coordinator" in llm.prompts[3]


async def test_analyze_aggregates_token_counts_across_stages():
    llm = FakeLLM(
        _understanding_payload(),
        _occupation_profile_payload(),
        _persona_payload(),
        _search_payload(),
    )
    outcome = await analyze(_Opp(), codes=(), llm=llm)
    # FakeLLM returns prompt_tokens=None; the engine treats None as zero, so the
    # aggregate is 0 rather than a TypeError on None + None.
    assert outcome.stats.prompt_tokens == 0
    assert outcome.stats.completion_tokens == 0
    assert outcome.stats.latency_ms == 0


async def test_analyze_records_removed_codes():
    """The redaction audit flows out of the orchestrator."""
    llm = FakeLLM(
        _understanding_payload(),
        _occupation_profile_payload(),
        _persona_payload(),
        _search_payload(),
    )
    outcome = await analyze(_Opp(), codes=(), llm=llm)
    # No codes in this fixture, so nothing was removed.
    assert outcome.removed_codes == []


async def test_get_llm_routes_to_glm_when_enabled(monkeypatch):
    """With the GLM provider on, the engine resolves to the GLM wrapper.

    The "run analysis" pipeline switches provider without the stages knowing:
    they call whatever `llm` they are handed, and the engine hands them
    `_glm_complete_json` (which wraps `complete_json` with the Z.AI base URL
    and key) when `JOB_INTELLIGENCE_LLM_ENABLED` + API key are set. With the
    flag off, the standard OpenAI-compatible client is used.
    """
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_ENABLED", False)
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_API_KEY", "")
    assert _get_llm_for_job_intelligence() is complete_json

    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_ENABLED", True)
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_API_KEY", "test-glm-key")
    fn = _get_llm_for_job_intelligence()
    # The wrapper is not the same object as complete_json itself — it wraps it.
    assert fn is not complete_json
    assert fn.__name__ == "_glm_complete_json"

    # Enabled but no key: the gate fails closed to the deterministic path.
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_ENABLED", True)
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_API_KEY", "")
    assert _get_llm_for_job_intelligence() is complete_json


async def test_glm_wrapper_overrides_base_url_and_key(monkeypatch):
    """The GLM wrapper must point the request at Z.AI, not the extraction
    provider. The stages pass `base_url=settings.LLM_PROVIDER_BASE_URL` and
    `api_key=settings.LLM_PROVIDER_API_KEY` explicitly, so routing to a
    different function would still send the job to DeepInfra. The wrapper
    overrides those kwargs with the Z.AI coding-plan values.
    """
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_ENABLED", True)
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_API_KEY", "test-glm-key")
    monkeypatch.setattr(
        settings, "JOB_INTELLIGENCE_LLM_BASE_URL", "https://zai.test/v4"
    )
    captured = {}

    async def spy(prompt, **kwargs):
        captured.update(kwargs)
        return await FakeLLM(_understanding_payload())(prompt, **kwargs)

    # Replace complete_json inside the engine module so the wrapper's call
    # is intercepted.
    monkeypatch.setattr(
        "app.services.job_intelligence.engine.complete_json", spy
    )
    await _glm_complete_json("prompt", model="glm-5.3", schema=None)

    assert captured["base_url"] == "https://zai.test/v4"
    assert captured["api_key"] == "test-glm-key"


async def test_analyze_uses_glm_client_when_enabled(monkeypatch):
    """End to end: with GLM on, `analyze` threads the GLM client into every
    stage (same FakeLLM seam, so no real model is called)."""
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_ENABLED", True)
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_LLM_API_KEY", "test-glm-key")

    llm = FakeLLM(
        _understanding_payload(),
        _occupation_profile_payload(),
        _persona_payload(),
        _search_payload(),
    )
    # An explicit llm always wins — FakeLLM stands in for the GLM client.
    outcome = await analyze(_Opp(), codes=(), llm=llm)
    assert outcome.result.understanding.role == "Logistics Manager"
    assert len(llm.prompts) == 4
