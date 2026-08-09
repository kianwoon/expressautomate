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
from sqlalchemy import text as sa_text

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
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
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

# allow-hardcode: a JD context stub the re-rank tests pass; the tests assert on
# the pick/confidence logic, not on prompt wording.
_CTX = "Senior Accounts Executive at an energy trading firm."

_PROF = OccupationProfile(
    occupation="Software Developer",
    functions={},
    seniority="Mid",
    people_management=False,
    industry="Technology",
)


async def test_rerank_picks_matching_title_and_folds_wages():
    llm = FakeLLM({"title": "software developer", "confidence": 0.9, "rationale": "fits"})
    picked = await rerank_occupation(_CTX, _PROF, _candidates(), llm=llm)
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
    picked = await rerank_occupation(_CTX, _PROF, _candidates(), llm=llm)
    assert picked is None


async def test_rerank_returns_none_for_empty_candidates():
    picked = await rerank_occupation(_CTX, _PROF, [], llm=FakeLLM())
    assert picked is None


async def test_rerank_returns_none_below_confidence_floor():
    """A sub-threshold confidence hides the benchmark rather than mislead."""
    llm = FakeLLM({"title": "software developer", "confidence": 0.3, "rationale": "weak"})
    picked = await rerank_occupation(_CTX, _PROF, _candidates(), llm=llm)
    assert picked is None


async def test_rerank_matches_case_insensitively():
    llm = FakeLLM({"title": "Software Developer", "confidence": 0.7, "rationale": "ok"})
    picked = await rerank_occupation(_CTX, _PROF, _candidates(), llm=llm)
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
# not catch because they short-circuit before the query. It seeds its own
# occupation rows (CI's test DB has an empty mom_occupations — the migration
# creates the table but the seed script is a manual deploy step), then cleans
# them up.


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(v)) for v in values) + "]"


async def _seed_occupations(admin_session, rows: list[dict]) -> None:
    """Insert test occupation rows under the admin role (BYPASSRLS).

    `mom_occupations` has no DML policy (writes are the seed script's job under
    the admin role), so a tenant session cannot insert — the admin session is
    the only writer, matching production. The vector literal is inlined (not
    bound) for the same asyncpg reason as the search query: the `::vector` cast
    in a bound parameter is a syntax error under asyncpg.
    """
    for r in rows:
        emb_literal = _vec_literal(r["embedding"])
        await admin_session.execute(
            sa_text(
                "INSERT INTO mom_occupations "
                "(id, year, title, gross_p25, gross_p50, gross_p75, "
                " basic_p25, basic_p50, basic_p75, embedding) "
                "VALUES (:id, :year, :title, :gp25, :gp50, :gp75, "
                f"        :bp25, :bp50, :bp75, '{emb_literal}'::vector)"
            ),
            {
                "id": str(r["id"]),
                "year": r["year"],
                "title": r["title"],
                "gp25": r["gross_p25"],
                "gp50": r["gross_p50"],
                "gp75": r["gross_p75"],
                "bp25": r["basic_p25"],
                "bp50": r["basic_p50"],
                "bp75": r["basic_p75"],
            },
        )


@pytest.fixture(autouse=True)
def _embeddings_enabled(monkeypatch):
    """Force `embedding_configured()` true so the search path isn't skipped.

    Also stubs `embed_one` to return a fixed vector, so the test exercises the
    SQL query (its real purpose) without a network call. The fixed vector is a
    unit vector pointing the same way as the seeded rows, so the cosine search
    returns them ranked.
    """
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://embed.test/v1")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")

    from app.services.job_intelligence import occupation as occ_module

    # allow-hardcode: a 1536-dim unit vector. The seeded rows share this
    # direction, so cosine similarity is ~1.0 and the search returns them.
    fixed = [1.0] + [0.0] * (settings.EMBEDDING_DIM - 1)

    async def _fake_embed_one(_text, **_kwargs):
        return fixed

    monkeypatch.setattr(occ_module, "embed_one", _fake_embed_one)


async def test_search_runs_the_vector_query_against_a_real_session(admin_session):
    """The raw SQL executes under a tenant session and returns ranked rows.

    A regression here (ArgumentError from a bare string, asyncpg syntax error
    from a `::vector` bind param, NULL embeddings wiped by a bad upsert) fails
    this test rather than the recruiter's Run-analysis button in prod.
    """
    # allow-hardcode: two occupations aligned with the stubbed query vector.
    seeded = [
        {
            "id": uuid.uuid4(), "year": 2024, "title": "software developer",
            "gross_p25": 6658, "gross_p50": 8888, "gross_p75": 13513,
            "basic_p25": 6599, "basic_p50": 8750, "basic_p75": 12574,
            "embedding": [1.0] + [0.0] * (settings.EMBEDDING_DIM - 1),
        },
        {
            "id": uuid.uuid4(), "year": 2024, "title": "data scientist",
            "gross_p25": 6890, "gross_p50": 9047, "gross_p75": 12132,
            "basic_p25": 6867, "basic_p50": 8992, "basic_p75": 12055,
            "embedding": [0.9] + [0.1] + [0.0] * (settings.EMBEDDING_DIM - 2),
        },
    ]
    seeded_ids = [str(r["id"]) for r in seeded]
    await _seed_occupations(admin_session, seeded)
    await admin_session.commit()

    try:
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
    finally:
        await admin_session.execute(
            sa_text("DELETE FROM mom_occupations WHERE id = ANY(:ids)"),
            {"ids": seeded_ids},
        )
        await admin_session.commit()
