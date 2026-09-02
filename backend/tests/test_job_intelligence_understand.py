"""Module 1 — understand the work. Prompt shape and output parsing.

No test here reaches a model: `llm=` takes the `FakeLLM`. The autouse fixture
pins the settings the module reads so the suite does not depend on the ambient
environment.

allow-hardcode: the model id, context text and model responses below are
fixtures, not an oracle and not configuration.
"""

import pytest

from app.core.config import settings
from app.services.job_intelligence.understand import build_prompt, understand
from app.services.llm.client import FakeLLM, LLMResult

_CONTEXT = "Job title: Logistics Manager\nJob description: Run the warehouse day shift."


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


def _full_payload(**overrides):
    base = {
        "role": "Logistics Manager",
        "business_purpose": "Keep the warehouse running",
        "daily_activities": ["Plan dispatch", "Manage a team of 8"],
        "work_environment": "Warehouse, day shift",
        "must_have_requirements": ["3 years logistics"],
        "preferred_requirements": ["SAP"],
        "working_conditions": "Onsite, 6-day week",
        "success_characteristics": ["Calm under pressure"],
        "potential_challenges": ["Long hours in peak season"],
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


def test_build_prompt_contains_the_schema_and_the_context():
    prompt = build_prompt(_CONTEXT)
    # The schema the model must match travels in the prompt.
    assert "business_purpose" in prompt
    assert "daily_activities" in prompt
    assert "confidence" in prompt
    # The redacted context is what the model actually reads.
    assert _CONTEXT in prompt


def test_build_prompt_explains_the_redaction_marker():
    """A withheld requirement must read as withheld, not as a skill."""
    prompt = build_prompt("")
    assert "[redacted]" in prompt
    assert "withheld for legal reasons" in prompt


def test_build_prompt_lets_typical_title_knowledge_through():
    """The understand stage must not order the model into bare disclaimers.

    A title-only order answered with "duties not described in the job order"
    made the Run-anyway escape hatch useless: the recruiter pressed it to learn
    what the title typically means. The prompt now splits its demands —
    order-specific facts stay grounded in the order (anti-fabrication §15),
    while `role`/`business_purpose`/`daily_activities` may draw on what the
    title denotes, phrased as typical. Confidence still scores the ORDER, so a
    rich write-up over a thin order cannot masquerade as a well-grounded one.
    """
    prompt = build_prompt("")
    # The typical-knowledge allowance, with its phrasing guard.
    assert "typically" in prompt
    assert "never present it as" in prompt
    # The honesty guard: confidence scores the order, not the write-up.
    assert "not how full your write-up is" in prompt
    # Order-specific facts stay grounded.
    assert "must come from the order" in prompt


async def test_understand_returns_a_validated_understanding():
    payload = _full_payload()
    llm = FakeLLM(payload)
    result, llm_result = await understand(_CONTEXT, llm=llm)
    assert result.role == "Logistics Manager"
    assert result.daily_activities == ["Plan dispatch", "Manage a team of 8"]
    assert result.confidence == 0.8
    # The context reached the model verbatim.
    assert _CONTEXT in llm.prompts[0]
    assert llm_result.data == payload


async def test_understand_resolves_the_model_from_settings(monkeypatch):
    """JOB_INTELLIGENCE_MODEL wins; empty falls back to EXTRACTION_MODEL_FAST."""
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_MODEL", "test/intel")
    captured = {}

    async def _capture(prompt, *, model, schema, **_):
        captured["model"] = model
        return LLMResult(data=_full_payload(), model=model)

    await understand(_CONTEXT, llm=_capture)
    assert captured["model"] == "test/intel"

    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_MODEL", "")
    captured.clear()
    await understand(_CONTEXT, llm=_capture)
    assert captured["model"] == "test/fast"


async def test_understand_coerces_a_joined_string_challenges_field():
    """Regression for the 2026-08-27 production failure.

    The model returned `potential_challenges` as one joined sentence instead
    of a list — `Input should be a valid list [type=list_type]` killed a paid
    analysis at parse time (`job_intelligence_failed` in arq). The engine now
    splits it rather than failing.
    """
    payload = _full_payload(
        potential_challenges="High volumes of KYC/SOW applications; tight "
        "6-month engagement; compliance scrutiny"
    )
    llm = FakeLLM(payload)
    result, _ = await understand(_CONTEXT, llm=llm)
    assert result.potential_challenges == [
        "High volumes of KYC/SOW applications",
        "tight 6-month engagement",
        "compliance scrutiny",
    ]
