"""The occupation-match stage — LLM profile extraction, re-ranking, degradation.

Tests the three steps in isolation and the `classify_occupation` orchestrator,
using a `FakeLLM` for the two LLM calls and a stub for the pgvector search.
The search is not unit-tested here (it is raw SQL over a real table, exercised
by the integration suite); what matters is the stage's logic around it — the
extraction prompt, the candidate-to-match folding, and the graceful
degradation when there is nothing to search.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

import pytest

from app.core.config import settings
from app.services.job_intelligence.occupation import (
    _profile_for_embedding,
    classify_occupation,
    extract_occupation_profile,
    rerank_occupation,
    search_occupations,
)
from app.services.job_intelligence.schema import (
    JDUnderstanding,
    OccupationProfile,
)
from app.services.llm.client import FakeLLM


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


def _understanding():
    return JDUnderstanding(
        role="Software Developer",
        business_purpose="Build features",
        daily_activities=["Write code"],
        work_environment="Office",
        must_have_requirements=["Python"],
        preferred_requirements=[],
        working_conditions="Remote ok",
        success_characteristics=[],
        potential_challenges=[],
        confidence=0.9,
    )


def _profile_payload():
    return {
        "occupation": "Software Developer",
        "seniority": "Mid",
        "people_management": False,
        "industry": "Technology",
        "functions": {"Development": 70, "Design": 30},
    }


def _candidates():
    # allow-hardcode: plausible MOM survey rows as the search would return them.
    return [
        {
            "title": "software developer",
            "year": 2024,
            "gross_p25": 6658.0,
            "gross_p50": 8888.0,
            "gross_p75": 13513.0,
            "basic_p25": 6599.0,
            "basic_p50": 8750.0,
            "basic_p75": 12574.0,
            "similarity": 0.92,
        },
        {
            "title": "web and mobile applications developer",
            "year": 2024,
            "gross_p25": 6050.0,
            "gross_p50": 7000.0,
            "gross_p75": 9314.0,
            "basic_p25": 6050.0,
            "basic_p50": 7000.0,
            "basic_p75": 9299.0,
            "similarity": 0.85,
        },
    ]


# --------------------------------------------------------------------------- #
# Step 1 — extract profile
# --------------------------------------------------------------------------- #


async def test_extract_profile_returns_validated_model():
    llm = FakeLLM(_profile_payload())
    profile, result = await extract_occupation_profile(
        "some context", _understanding(), llm=llm
    )
    assert profile.occupation == "Software Developer"
    assert profile.people_management is False
    assert profile.functions == {"Development": 70, "Design": 30}
    # The understanding's role appears in the prompt the model was given.
    assert "Software Developer" in llm.prompts[0]
    assert result.data == _profile_payload()


def test_profile_for_embedding_drops_percentages_and_general_industry():
    """The embedding text leads with the occupation name, drops numeric noise."""
    profile = OccupationProfile(
        occupation="Manager",
        functions={"Recruitment": 50, "Admin": 50},
        seniority="Manager",
        people_management=True,
        industry="General",
    )
    text = _profile_for_embedding(profile)
    assert "Manager" in text
    assert "people management" in text
    # "General" industry adds no signal; percentages are noise vs short titles.
    assert "General" not in text
    assert "50" not in text


# --------------------------------------------------------------------------- #
# Step 2 — search degradation
# --------------------------------------------------------------------------- #


async def test_search_returns_empty_without_session():
    """No session (tests / no DB) means no candidates, not an error."""
    profile = OccupationProfile(
        occupation="Software Developer",
        functions={},
        seniority="Mid",
        people_management=False,
        industry="Technology",
    )
    assert await search_occupations(None, profile) == []


# --------------------------------------------------------------------------- #
# Step 3 — re-rank
# --------------------------------------------------------------------------- #


async def test_rerank_picks_matching_title_and_folds_wages():
    llm = FakeLLM({"title": "software developer", "confidence": 0.9, "rationale": "fits"})
    picked = await rerank_occupation(
        OccupationProfile(
            occupation="Software Developer",
            functions={},
            seniority="Mid",
            people_management=False,
            industry="Technology",
        ),
        _candidates(),
        llm=llm,
    )
    assert picked is not None
    match, result = picked
    # Wages come from the candidate row, never the model.
    assert match.title == "software developer"
    assert match.gross_p50 == 8888.0
    assert match.confidence == 0.9
    assert match.rationale == "fits"
    assert result.data["title"] == "software developer"


async def test_rerank_returns_none_for_fabricated_title():
    """A title the model invented (not in the list) is a non-match."""
    llm = FakeLLM({"title": "Software Engineer", "confidence": 0.8, "rationale": "x"})
    picked = await rerank_occupation(
        OccupationProfile(
            occupation="Software Developer",
            functions={},
            seniority="Mid",
            people_management=False,
            industry="Technology",
        ),
        _candidates(),
        llm=llm,
    )
    assert picked is None


async def test_rerank_returns_none_for_empty_candidates():
    picked = await rerank_occupation(
        OccupationProfile(
            occupation="Software Developer",
            functions={},
            seniority="Mid",
            people_management=False,
            industry="Technology",
        ),
        [],
        llm=FakeLLM(),
    )
    assert picked is None


async def test_rerank_matches_case_insensitively():
    llm = FakeLLM({"title": "Software Developer", "confidence": 0.7, "rationale": "ok"})
    picked = await rerank_occupation(
        OccupationProfile(
            occupation="Software Developer",
            functions={},
            seniority="Mid",
            people_management=False,
            industry="Technology",
        ),
        _candidates(),
        llm=llm,
    )
    assert picked is not None
    assert picked[0].title == "software developer"  # the survey's lower-cased title


# --------------------------------------------------------------------------- #
# Orchestrator — classify_occupation
# --------------------------------------------------------------------------- #


async def test_classify_returns_none_when_search_yields_nothing():
    """session=None → no candidates → graceful None, not an error."""
    llm = FakeLLM(_profile_payload())
    match, result = await classify_occupation(
        "context", _understanding(), session=None, llm=llm
    )
    assert match is None
    # The profile extraction still ran (its result is returned for cost summing).
    assert result is not None
