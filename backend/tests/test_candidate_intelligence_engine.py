"""The orchestrator — both stages in sequence, one fake model.

`analyze_candidate` threads one `llm` through both stages, so a single `FakeLLM`
queued with two responses runs the whole pipeline.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

from dataclasses import dataclass

import pytest

from app.core.config import settings
from app.services.candidate_intelligence.engine import analyze_candidate
from app.services.llm.client import FakeLLM


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@dataclass
class _Role:
    title: str = "Underwriter"
    employer: str = "Integral Plus"
    started_on: object = None
    ended_on: object = None
    description: str = ""
    status: str = "confirmed"


@dataclass
class _Candidate:
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


def _work_payload():
    # allow-hardcode: test fixture content.
    return {
        "roles": [
            {
                "employer": "Insurer B",
                "period": "2019-2023",
                "stated_title": "Senior Executive",
                "industry": "Insurance",
                "work_units": [
                    {
                        "claim": "Managed strategic underwriting portfolio",
                        "work": "Applied standard underwriting rules to submissions",
                        "decision_ownership": "2",
                        "complexity": "operational",
                        "ai_heavy_lift": "ai_heavy_lift",
                        "human_residual": "Exception review",
                        "evidence": "C",
                        "evidence_note": "Claims management, work is rule application.",
                        "inflated": True,
                    }
                ],
                "contribution_maturity": "independent",
                "tenure_months": 42,
            }
        ],
        "education": [
            {
                "period": "2012-2014",
                "qualification": "Diploma in Business Management",
                "institution": "SIM",
                "field": "Business",
            }
        ],
    }


def _assessment_payload():
    # allow-hardcode: test fixture content.
    return {
        "headline": "A routine operator with a senior title; AI-compressed work.",
        "summary": "Most work is rule application. No evidence of owning decisions.",
        "work_level": "operational",
        "decision_authority": "Level 2 — recommends; no evidence of owning decisions.",
        "scarce_capabilities": [],
        "depreciated_capabilities": [
            {
                "capability": "Standard underwriting processing",
                "reason": "Rules engines and AI handle standard decisions.",
            }
        ],
        "unproven_claims": [
            {
                "claim": "Managed strategic underwriting portfolio",
                "question": "Describe a decision you owned, not just processed.",
            }
        ],
        "ai_exposure": "AI does the heavy lift of standard underwriting.",
        "hire_readiness": "Immediate on routine; needs supervision for judgment.",
        "value_trajectory": "Declining — commoditized work.",
    }


async def test_analyze_runs_both_stages():
    llm = FakeLLM(_work_payload(), _assessment_payload())
    outcome = await analyze_candidate(
        _Candidate(),
        roles=[_Role()],
        skills=[],
        cv_text="Underwriting experience.",
        llm=llm,
    )
    assert outcome.result.work.roles[0].stated_title == "Senior Executive"
    assert outcome.result.work.roles[0].work_units[0].inflated is True
    assert outcome.result.assessment.work_level == "operational"
    assert len(llm.prompts) == 2


async def test_pipeline_carries_work_into_assessment_prompt():
    """The work decomposition flows into the assessment prompt."""
    llm = FakeLLM(_work_payload(), _assessment_payload())
    await analyze_candidate(_Candidate(), roles=[_Role()], llm=llm)
    assert "Senior Executive" in llm.prompts[1]
    assert "underwriting rules" in llm.prompts[1]


async def test_inflated_claim_detected():
    """The claim-vs-substance test flags inflated CV language."""
    llm = FakeLLM(_work_payload(), _assessment_payload())
    outcome = await analyze_candidate(_Candidate(), roles=[_Role()], llm=llm)
    wu = outcome.result.work.roles[0].work_units[0]
    assert wu.inflated is True
    assert wu.evidence == "C"
    assert wu.claim != wu.work


async def test_cv_text_reaches_work_prompt():
    llm = FakeLLM(_work_payload(), _assessment_payload())
    await analyze_candidate(_Candidate(), cv_text="UNIQUE CV MARKER", llm=llm)
    assert "UNIQUE CV MARKER" in llm.prompts[0]


async def test_aggregates_token_counts():
    llm = FakeLLM(_work_payload(), _assessment_payload())
    outcome = await analyze_candidate(_Candidate(), llm=llm)
    assert outcome.stats.prompt_tokens == 0
    assert outcome.stats.latency_ms == 0


async def test_both_stages_use_the_candidate_budget_not_extractions():
    """The work pass is the deepest reasoning prompt in the system and must not
    silently share extraction's `max_tokens`: deepseek-v4-flash counts reasoning
    against the budget, and 16000 was consumed by reasoning alone on a real run,
    returning no content. Each stage carries the candidate-specific knobs."""
    seen: list[dict] = []

    async def spy(prompt, *, model, schema, **kwargs):
        seen.append(dict(kwargs, model=model, schema=schema))
        return await FakeLLM(
            _work_payload(), _assessment_payload()
        )(prompt, model=model, schema=schema)

    await analyze_candidate(_Candidate(), roles=[_Role()], llm=spy)

    assert len(seen) == 2
    for call in seen:
        expected_model = settings.CANDIDATE_INTELLIGENCE_MODEL or settings.EXTRACTION_MODEL_FAST
        assert call["model"] == expected_model
        assert call["base_url"] == settings.DEEPSEEK_BASE_URL
        assert call["api_key"] == settings.DEEPSEEK_API_KEY
        assert call["schema"] is None
        assert call["extra_body"]["max_tokens"] == settings.CANDIDATE_INTELLIGENCE_MAX_TOKENS
        assert (
            call["extra_body"]["reasoning_effort"]
            == settings.CANDIDATE_INTELLIGENCE_REASONING_EFFORT
        )
