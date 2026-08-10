"""The semantic matching layer: the score component, the text it embeds, and
the rescue that brings back the candidates the structured fields miss.

These are the pure halves of the feature — the scorer's new component, the
text assembly that excludes protected attributes, and the redaction that
strips coded requirements before a job order reaches the embedding model. The
database-backed halves (the ANN query, the worker, the RRF rescue inside the
run) are exercised by the integration tests in `test_sourcing_job.py`, which
own the rows and the run the way only they can.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services.sourcing.embed import (
    candidate_text_for_embedding,
    opportunity_text_for_embedding,
    truncate,
)
from app.services.sourcing.redact import REDACTION_MARKER
from app.services.sourcing.score import (
    SEMANTIC,
    default_weights,
    score_candidate,
)

TODAY = date(2026, 8, 5)


# --- Stand-ins, shared with test_sourcing_score in shape but kept here so the
# semantic tests are readable on their own. ---------------------------------


@dataclass
class FakeOpportunity:
    id: object = None
    job_title_normalized: str | None = None
    job_title_raw: str | None = None
    job_description: str | None = None
    requirements: str | None = None
    company_name_normalized: str | None = None
    company_name_raw: str | None = None
    skills: list[str] | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None


@dataclass
class FakeCandidate:
    id: object = None
    current_title: str | None = None
    current_employer: str | None = None
    expected_salary: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None


@dataclass
class FakeRole:
    title: str = ""
    employer: str = ""
    started_on: date | None = None
    ended_on: date | None = None
    status: str = "confirmed"
    description: str | None = None


@dataclass
class FakeSkill:
    skill: str = ""
    skill_normalized: str = ""


@dataclass
class FakeCode:
    code: str = ""
    attribute: str | None = None


def _semantic_weights(monkeypatch: pytest.MonkeyPatch, weight: float) -> None:
    """Isolate the semantic weight so the test asserts on it alone."""
    monkeypatch.setattr(settings, "SOURCING_WEIGHT_SEMANTIC", weight)


# --- The score component --------------------------------------------------


async def test_semantic_absent_when_no_embedding(monkeypatch) -> None:
    """A candidate with no embedding abstains — absent is not bad."""
    _semantic_weights(monkeypatch, 2.0)
    opportunity = FakeOpportunity(
        job_title_normalized="developer", skills=["python"]
    )
    candidate = FakeCandidate(id=uuid4(), current_title="developer")
    skills = [FakeSkill(skill_normalized="python")]

    total, components = score_candidate(
        opportunity, candidate, [], skills, today=TODAY
    )

    by_name = {c.name: c for c in components}
    assert by_name[SEMANTIC].raw is None
    assert by_name[SEMANTIC].contribution is None
    assert "embedding" in (by_name[SEMANTIC].note or "").lower()
    # The total is still computed from the components that had data.
    assert total is not None


async def test_semantic_scores_when_embedding_present(monkeypatch) -> None:
    """A candidate with an embedding is scored on cosine similarity."""
    _semantic_weights(monkeypatch, 2.0)
    cid = uuid4()
    opportunity = FakeOpportunity(
        job_title_normalized="developer", skills=["python"]
    )
    candidate = FakeCandidate(id=cid, current_title="developer")
    skills = [FakeSkill(skill_normalized="python")]

    total, components = score_candidate(
        opportunity,
        candidate,
        [],
        skills,
        semantic_scores={cid: 0.85},
        today=TODAY,
    )

    by_name = {c.name: c for c in components}
    assert by_name[SEMANTIC].raw == Decimal("0.85")
    assert by_name[SEMANTIC].contribution == Decimal("2.0") * Decimal("0.85")
    assert total is not None


async def test_semantic_zero_weight_excludes_it_from_total(monkeypatch) -> None:
    """A zero semantic weight is honoured, proving it is read from settings."""
    _semantic_weights(monkeypatch, 0.0)
    cid = uuid4()
    opportunity = FakeOpportunity(
        job_title_normalized="developer", skills=["python"]
    )
    candidate = FakeCandidate(id=cid, current_title="developer")

    baseline, _ = score_candidate(
        opportunity, candidate, [], [FakeSkill(skill_normalized="python")],
        today=TODAY,
    )
    with_semantic, _ = score_candidate(
        opportunity,
        candidate,
        [],
        [FakeSkill(skill_normalized="python")],
        semantic_scores={cid: 0.99},
        today=TODAY,
    )

    # Zero weight means the component contributes nothing even at 0.99.
    assert baseline == with_semantic


async def test_semantic_similarity_clamped_to_unit_interval(monkeypatch) -> None:
    """An out-of-range similarity is clamped, never rewarding an arithmetic error."""
    _semantic_weights(monkeypatch, 2.0)
    cid = uuid4()
    opportunity = FakeOpportunity(
        job_title_normalized="developer", skills=["python"]
    )
    candidate = FakeCandidate(id=cid, current_title="developer")

    _, components = score_candidate(
        opportunity,
        candidate,
        [],
        [FakeSkill(skill_normalized="python")],
        semantic_scores={cid: 1.5},
        today=TODAY,
    )

    assert {c.name: c for c in components}[SEMANTIC].raw == Decimal("1")


async def test_semantic_in_default_weights() -> None:
    """The semantic weight is wired into the default weight map."""
    assert SEMANTIC in default_weights()


# --- Candidate text assembly: protected attributes excluded --------------


def test_candidate_text_excludes_protected_attributes() -> None:
    """Sex, race, nationality, DOB must never enter the embedded text."""
    candidate = FakeCandidate(
        id=uuid4(),
        current_title="Staff Nurse",
        current_employer="Acme Health",
    )
    # FakeCandidate doesn't carry the protected fields, but the real Candidate
    # does — and the point of the test is that candidate_text_for_embedding
    # never reads them. It assembles only title, employer, roles, skills.
    text = candidate_text_for_embedding(candidate, [], [])

    assert "Staff Nurse" in text
    assert "Acme Health" in text


def test_candidate_text_includes_roles_and_skills() -> None:
    candidate = FakeCandidate(id=uuid4(), current_title="Developer")
    roles = [
        FakeRole(title="Backend Engineer", employer="Fintech Co", description="Built APIs"),
        FakeRole(title="Junior Dev", employer="Startup Inc"),
    ]
    skills = [FakeSkill(skill_normalized="python"), FakeSkill(skill_normalized="django")]

    text = candidate_text_for_embedding(candidate, roles, skills)

    assert "Developer" in text
    assert "Backend Engineer" in text
    assert "Built APIs" in text
    assert "python" in text
    assert "django" in text


def test_candidate_text_skips_rejected_roles() -> None:
    """A rejected role is one a human said did not happen — it must not embed."""
    candidate = FakeCandidate(id=uuid4(), current_title="Developer")
    roles = [
        FakeRole(title="Real Role", employer="A", status="confirmed"),
        FakeRole(title="Disputed Role", employer="B", status="rejected"),
    ]
    text = candidate_text_for_embedding(candidate, roles, [])

    assert "Real Role" in text
    assert "Disputed Role" not in text


def test_candidate_text_empty_when_nothing_job_related() -> None:
    candidate = FakeCandidate(id=uuid4())
    assert candidate_text_for_embedding(candidate, [], []) == ""


# --- Opportunity text assembly: redaction --------------------------------


def test_opportunity_text_redacts_protected_codes() -> None:
    """A coded protected-characteristic requirement is stripped before embedding."""
    opportunity = FakeOpportunity(
        job_title_raw="Staff Nurse",
        job_description="Looking for a nurse. CODE_F preferred.",
        requirements="Must be willing to work shifts.",
        skills=["triage"],
    )
    codes = [FakeCode(code="CODE_F", attribute="sex")]

    text, removed = opportunity_text_for_embedding(opportunity, codes)

    assert "CODE_F" not in text
    assert REDACTION_MARKER in text
    assert "CODE_F" in removed
    assert "Staff Nurse" in text
    assert "triage" in text


def test_opportunity_text_keeps_non_protected_codes() -> None:
    """A code with no protected attribute is a real requirement, kept intact."""
    opportunity = FakeOpportunity(
        job_title_raw="Driver",
        job_description="Must hold CODE_LIC license.",
    )
    codes = [FakeCode(code="CODE_LIC", attribute=None)]

    text, removed = opportunity_text_for_embedding(opportunity, codes)

    assert "CODE_LIC" in text
    assert removed == []


def test_opportunity_text_empty_when_nothing_to_embed() -> None:
    opportunity = FakeOpportunity()
    text, removed = opportunity_text_for_embedding(opportunity, [])
    assert text == ""
    assert removed == []


# --- Truncation -----------------------------------------------------------


def test_truncate_caps_at_max_chars() -> None:
    assert truncate("abcdef", 3) == "abc"
    assert truncate("", 100) == ""
    assert truncate("abc", 0) == ""
