"""Module 3 — generate the search strategy.

The search stage is fed BOTH the understanding and the persona, so its queries
aim at the people Module 2 described. This test asserts that contract.

No test here reaches a model.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

import pytest

from app.core.config import settings
from app.services.job_intelligence.schema import (
    CandidatePersona,
    JDUnderstanding,
)
from app.services.job_intelligence.search import build_prompt, plan_search
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
        daily_activities=["Plan dispatch"],
        work_environment="Warehouse, day shift",
        must_have_requirements=["3 years logistics"],
        preferred_requirements=[],
        working_conditions="Onsite, 6-day week",
        success_characteristics=[],
        potential_challenges=[],
        confidence=0.8,
    )


def _persona():
    return CandidatePersona(
        likely_backgrounds=["Logistics coordinator"],
        transferable_roles=["Supply chain analyst"],
        transferable_industries=["Freight"],
        behaviours=["Calm under pressure"],
        communication_style="Direct and practical",
        career_stage="Mid-level, 5-8 years",
        motivations=["Stable roster"],
        salary_expectation="SGD 4,000 - 5,500 / month",
        availability="1 month notice",
    )


def _search_payload(**overrides):
    base = {
        "platform": "LinkedIn",
        "priority": 2,
        "queries": [
            '("logistics manager" OR "warehouse supervisor") AND dispatch',
        ],
        "negative_queries": ["intern", "student"],
        "salary": "SGD 4,000 - 5,500 / month",
        "location": "Singapore",
        "employment_type": "Full-time",
    }
    base.update(overrides)
    return base


def test_build_prompt_carries_understanding_persona_and_context():
    prompt = build_prompt(
        _CONTEXT,
        "Role: Logistics Manager\nPurpose: Keep it running",
        "Backgrounds: Logistics coordinator\nCareer stage: Mid-level",
    )
    assert "Logistics Manager" in prompt
    assert "Logistics coordinator" in prompt
    assert _CONTEXT in prompt
    assert "[redacted]" in prompt


async def test_plan_search_parses_and_feeds_both_stages():
    payload = _search_payload()
    llm = FakeLLM(payload)
    result, _ = await plan_search(_CONTEXT, _understanding(), _persona(), llm=llm)
    assert result.platform == "LinkedIn"
    assert result.queries == [
        '("logistics manager" OR "warehouse supervisor") AND dispatch',
    ]
    assert result.negative_queries == ["intern", "student"]
    # Both upstream stages reached the prompt.
    assert "Logistics Manager" in llm.prompts[0]
    assert "Logistics coordinator" in llm.prompts[0]


async def test_plan_search_refuses_protected_characteristic_filters():
    payload = _search_payload()
    llm = FakeLLM(payload)
    await plan_search(_CONTEXT, _understanding(), _persona(), llm=llm)
    assert "protected characteristic" in llm.prompts[0]


def test_search_schema_types_priority_as_integer():
    """`priority` is an int in the Pydantic model, so the schema must ask for an integer.

    Regression test: the schema once treated `priority` as an array of strings
    (the default), so the model returned `["1"]` and the parser rejected it with
    "Input should be a valid integer". The schema and the Pydantic model must
    agree on every field's type.
    """
    from app.services.job_intelligence.schema import json_schema

    search = json_schema()["search"]
    assert search["properties"]["priority"] == {"type": "integer"}
    # And the surrounding fields keep their own types.
    assert search["properties"]["platform"] == {"type": "string"}
    assert search["properties"]["queries"] == {
        "type": "array",
        "items": {"type": "string"},
    }


async def test_plan_search_parses_a_bare_integer_priority():
    """A model that returns `priority: 1` (the integer the schema asks for) parses."""
    payload = _search_payload(priority=1)
    llm = FakeLLM(payload)
    result, _ = await plan_search(_CONTEXT, _understanding(), _persona(), llm=llm)
    assert result.priority == 1
    assert isinstance(result.priority, int)
