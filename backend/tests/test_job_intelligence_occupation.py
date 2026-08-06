"""The occupation-match stage — LLM profile extraction, re-ranking, degradation.

Tests the three steps in isolation and the `classify_occupation` orchestrator,
using a `FakeLLM` for the two LLM calls. The pgvector search is exercised
against the real test database (which the seed script populates with 562 MOM
occupations) — this is the test that catches a raw-SQL regression before it
reaches prod, because the search path is otherwise short-circuited to `[]`
when `session is None`.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

import uuid

import pytest

from app.core.config import settings
from app.db.rls import tenant_session
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


# --------------------------------------------------------------------------- #
# Integration — search_occupations against the real test database
# --------------------------------------------------------------------------- #
# This test runs the raw pgvector SQL through a real tenant session. It is the
# guard against the class of bug that shipped to prod: a `text()` / asyncpg
# incompatibility in the vector cast that the unit tests (session=None) could
# not catch because they short-circuit before the query. It requires the seed
# script to have populated `mom_occupations` in the test DB (the test-env
# migration + a one-off `seed_mom_occupations.py --write`).


@pytest.fixture(autouse=True)
def _embeddings_enabled(monkeypatch):
    """Force `embedding_configured()` true so the search path isn't skipped.

    Also stubs `embed_one` to return a fixed vector, so the test exercises the
    SQL query (its real purpose) without making a network call to the embedding
    provider. The fixed vector is arbitrary — it need only be the right
    dimension to satisfy the `::vector(1536)` cast.
    """
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://embed.test/v1")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")

    from app.services.job_intelligence import occupation as occ_module

    # allow-hardcode: a 1536-dim vector of zeroes — not a meaningful search
    # query, just enough to exercise the SQL cast and ordering without a real
    # embedding. The test asserts on structure, not ranking.
    fixed = [0.01] * settings.EMBEDDING_DIM

    async def _fake_embed_one(_text, **_kwargs):
        return fixed

    monkeypatch.setattr(occ_module, "embed_one", _fake_embed_one)


async def test_search_runs_the_vector_query_against_a_real_session():
    """The raw SQL executes under a tenant session and returns ranked rows.

    A regression here (ArgumentError from a bare string, asyncpg syntax error
    from a `::vector` bind param, NULL embeddings wiped by a bad upsert) fails
    this test rather than the recruiter's Run-analysis button in prod.
    """
    profile = OccupationProfile(
        occupation="Software Developer",
        functions={"Development": 100},
        seniority="Mid",
        people_management=False,
        industry="Technology",
    )
    async with tenant_session(uuid.uuid4()) as session:
        results = await search_occupations(session, profile, k=5)

    assert len(results) > 0
    assert len(results) <= 5
    # Every result carries the wage columns the chart plots.
    first = results[0]
    assert "title" in first and first["title"]
    assert "gross_p25" in first and isinstance(first["gross_p25"], float)
    assert "gross_p50" in first
    assert "gross_p75" in first
    assert "similarity" in first and 0.0 <= first["similarity"] <= 1.0
