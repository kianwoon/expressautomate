"""Module 2 — infer the ideal candidate persona.

The persona stage is fed the work understanding (Module 1's output), not the
raw job order. This test asserts that contract: a canned `JDUnderstanding`
reaches the prompt as readable text, and the redacted context travels too.

No test here reaches a model.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

import pytest

from app.core.config import settings
from app.services.job_intelligence.persona import build_prompt, infer_persona
from app.services.job_intelligence.schema import JDUnderstanding
from app.services.llm.client import FakeLLM

_CONTEXT = "Job title: Logistics Manager\nJob description: Run the warehouse day shift."


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


def _understanding():
    return JDUnderstanding(
        role="Logistics Manager",
        business_purpose="Keep the warehouse running",
        daily_activities=["Plan dispatch", "Manage a team of 8"],
        work_environment="Warehouse, day shift",
        must_have_requirements=["3 years logistics"],
        preferred_requirements=[],
        working_conditions="Onsite, 6-day week",
        success_characteristics=[],
        potential_challenges=[],
        confidence=0.8,
    )


def _persona_payload(**overrides):
    base = {
        "likely_backgrounds": ["Logistics coordinator", "Warehouse supervisor"],
        "transferable_roles": ["Supply chain analyst"],
        "transferable_industries": ["Freight", "Retail distribution"],
        "behaviours": ["Calm under pressure", "Hands-on"],
        "communication_style": "Direct and practical",
        "career_stage": "Mid-level, 5-8 years",
        "motivations": ["Stable roster", "Team ownership"],
        "salary_expectation": "SGD 4,000 - 5,500 / month",
        "availability": "1 month notice",
    }
    base.update(overrides)
    return base


def test_build_prompt_carries_the_understanding_and_context():
    prompt = build_prompt(_CONTEXT, "Role: Logistics Manager\nPurpose: Keep it running")
    # The understanding feeds the persona stage.
    assert "Logistics Manager" in prompt
    # The redacted context travels for grounding.
    assert _CONTEXT in prompt
    # The redaction marker is explained.
    assert "[redacted]" in prompt


async def test_infer_persona_parses_and_feeds_the_understanding():
    payload = _persona_payload()
    llm = FakeLLM(payload)
    understanding = _understanding()
    result, _ = await infer_persona(_CONTEXT, understanding, llm=llm)
    assert result.transferable_roles == ["Supply chain analyst"]
    assert result.career_stage == "Mid-level, 5-8 years"
    # The understanding's role reached the prompt the model saw.
    assert "Logistics Manager" in llm.prompts[0]


async def test_infer_persona_refuses_protected_characteristics_language():
    """The prompt tells the model never to infer a protected characteristic."""
    payload = _persona_payload()
    llm = FakeLLM(payload)
    await infer_persona(_CONTEXT, _understanding(), llm=llm)
    assert "protected characteristic" in llm.prompts[0]
