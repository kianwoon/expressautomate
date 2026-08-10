"""What a candidate's score says, and what it refuses to say.

The scorer is pure, so these tests use plain stand-ins rather than rows: the
arithmetic and the abstention rules are the whole subject, and a database in
the way would only make them slower to read.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.sourcing.score import Component, score_candidate

TODAY = date(2026, 7, 29)


@dataclass
class FakeOpportunity:
    job_title_normalized: str | None = None
    job_title_raw: str | None = None
    company_name_normalized: str | None = None
    company_name_raw: str | None = None
    skills: list[str] | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None


@dataclass
class FakeCandidate:
    current_title: str | None = None
    expected_salary: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None


@dataclass
class FakeRole:
    title: str = ""
    employer: str = ""
    employer_normalized: str = ""
    started_on: date | None = None
    started_precision: str | None = "month"
    ended_on: date | None = None
    ended_precision: str | None = "month"
    status: str = "confirmed"


@dataclass
class FakeSkill:
    skill: str
    skill_normalized: str


def _skills(*names: str) -> list[FakeSkill]:
    return [FakeSkill(skill=n, skill_normalized=n.lower().strip()) for n in names]


def _by_name(components: list[Component]) -> dict[str, Component]:
    return {c.name: c for c in components}


def _full_case() -> tuple[FakeOpportunity, FakeCandidate, list[FakeRole], list[FakeSkill]]:
    opportunity = FakeOpportunity(
        job_title_normalized="staff nurse",
        company_name_normalized="acme health",
        company_name_raw="Acme Health",
        skills=["Triage", "IV Cannulation"],
        salary_min=4000.0,
        salary_max=6000.0,
        salary_currency="SGD",
        salary_period="month",
    )
    candidate = FakeCandidate(
        current_title="Senior Staff Nurse",
        expected_salary=5000.0,
        salary_currency="SGD",
        salary_period="month",
    )
    roles = [
        FakeRole(
            title="Senior Staff Nurse",
            employer="Acme Health Pte Ltd",
            employer_normalized="acme health",
            started_on=date(2018, 1, 1),
            ended_on=None,
        )
    ]
    return opportunity, candidate, roles, _skills("triage", "IV cannulation")


async def test_every_component_is_named_signed_and_weighted() -> None:
    opportunity, candidate, roles, skills = _full_case()
    total, components = score_candidate(
        opportunity, candidate, roles, skills, today=TODAY
    )

    assert total is not None
    assert {c.name for c in components} == {
        "title",
        "semantic",
        "skills",
        "employer",
        "salary",
        "tenure",
        "recency",
    }
    for component in components:
        assert component.weight >= 0
        if component.raw is None:
            assert component.note
        else:
            assert component.contribution == component.weight * component.raw


async def test_weights_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turning a weight to zero must change the total, or it was a literal."""
    opportunity, candidate, roles, skills = _full_case()
    baseline, _ = score_candidate(opportunity, candidate, roles, skills, today=TODAY)

    monkeypatch.setattr(settings, "SOURCING_WEIGHT_SKILLS", 100.0)
    tilted, components = score_candidate(
        opportunity, candidate, roles, skills, today=TODAY
    )

    assert _by_name(components)["skills"].weight == Decimal("100.0")
    assert tilted != baseline


async def test_a_component_with_no_data_is_noted_and_excluded_not_zeroed() -> None:
    """The salary band is missing, so salary abstains rather than scoring 0."""
    opportunity, candidate, roles, skills = _full_case()
    opportunity.salary_min = None
    opportunity.salary_max = None

    total, components = score_candidate(
        opportunity, candidate, roles, skills, today=TODAY
    )
    salary = _by_name(components)["salary"]

    assert salary.raw is None
    assert salary.contribution is None
    assert salary.note

    # Every other component here scores 1.0 except tenure and recency, which
    # are computed. Had salary been folded in as a zero the total would sit
    # below the mean of the components that actually had something to say.
    scored = [c for c in components if c.raw is not None]
    expected = sum((c.weight * c.raw for c in scored), Decimal(0)) / sum(
        (c.weight for c in scored), Decimal(0)
    )
    assert total == expected.quantize(total)


async def test_total_is_the_weighted_mean_of_components_with_data() -> None:
    opportunity, candidate, roles, skills = _full_case()
    total, components = score_candidate(
        opportunity, candidate, roles, skills, today=TODAY
    )

    scored = [c for c in components if c.raw is not None]
    numerator = sum((c.contribution for c in scored), Decimal(0))
    denominator = sum((c.weight for c in scored), Decimal(0))
    assert total == (numerator / denominator).quantize(total)


async def test_a_candidate_with_no_data_at_all_has_no_score() -> None:
    """Not zero. Zero reads as "unsuitable"; this means "we know nothing"."""
    total, components = score_candidate(
        FakeOpportunity(), FakeCandidate(), [], [], today=TODAY
    )

    assert total is None
    assert components
    assert all(c.raw is None and c.note for c in components)


async def test_the_same_inputs_produce_the_same_score_twice() -> None:
    opportunity, candidate, roles, skills = _full_case()
    first, first_components = score_candidate(
        opportunity, candidate, roles, skills, today=TODAY
    )
    second, second_components = score_candidate(
        opportunity, candidate, roles, skills, today=TODAY
    )

    assert first == second
    assert first_components == second_components


async def test_skills_match_on_normalised_forms_either_side() -> None:
    opportunity, candidate, roles, skills = _full_case()
    opportunity.skills = ["  TRIAGE  ", "IV Cannulation"]

    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)

    assert _by_name(components)["skills"].raw == Decimal(1)


async def test_c_and_c_plus_plus_are_not_the_same_skill() -> None:
    """`tokens()` strips punctuation and would collapse these into one word."""
    opportunity, candidate, roles, _ = _full_case()
    opportunity.skills = ["C++"]

    _, components = score_candidate(
        opportunity, candidate, roles, _skills("C"), today=TODAY
    )
    assert _by_name(components)["skills"].raw == Decimal(0)

    _, components = score_candidate(
        opportunity, candidate, roles, _skills("C++"), today=TODAY
    )
    assert _by_name(components)["skills"].raw == Decimal(1)


async def test_employer_signal_compares_normalised_company_names() -> None:
    opportunity, candidate, roles, skills = _full_case()
    # The role says "Acme Health Pte Ltd"; the job order says "Acme Health".
    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    assert _by_name(components)["employer"].raw == Decimal(1)

    opportunity.company_name_normalized = "other clinic"
    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    assert _by_name(components)["employer"].raw == Decimal(0)


async def test_employer_abstains_when_the_job_order_names_no_company() -> None:
    opportunity, candidate, roles, skills = _full_case()
    opportunity.company_name_normalized = None
    opportunity.company_name_raw = None

    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    employer = _by_name(components)["employer"]
    assert employer.raw is None and employer.note


async def test_employer_falls_back_to_raw_company_name() -> None:
    """The normalized column is never populated by ingestion, so a job order
    that named a company must not read as nameless. The raw name is the one
    the email actually carried (Woodlands Health, Acme Health, ...)."""
    opportunity, candidate, roles, skills = _full_case()
    opportunity.company_name_normalized = None
    # Same employer the candidate has on record → full match via the raw name.
    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    employer = _by_name(components)["employer"]
    assert employer.raw == Decimal(1)

    # A company the candidate has never worked for → a real 0, not an abstain.
    opportunity.company_name_raw = "Woodlands Health"
    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    employer = _by_name(components)["employer"]
    assert employer.raw == Decimal(0)
    assert "not worked" in (employer.note or "")


async def test_title_uses_containment_on_the_job_side() -> None:
    """A more specific candidate title is a full match, not a penalty."""
    opportunity, candidate, roles, skills = _full_case()
    _, components = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    assert _by_name(components)["title"].raw == Decimal(1)


async def test_tenure_and_recency_come_from_the_role_spans() -> None:
    opportunity, candidate, _, skills = _full_case()
    long_serving = [
        FakeRole(
            title="Staff Nurse",
            employer="Acme Health",
            employer_normalized="acme health",
            started_on=date(1990, 1, 1),
            ended_on=None,
        )
    ]
    _, components = score_candidate(
        opportunity, candidate, long_serving, skills, today=TODAY
    )
    named = _by_name(components)
    # Past the full-marks horizon, and still in post, so both peg at 1.0.
    assert named["tenure"].raw == Decimal(1)
    assert named["recency"].raw == Decimal(1)

    stale = [
        FakeRole(
            title="Staff Nurse",
            employer="Acme Health",
            employer_normalized="acme health",
            started_on=date(2000, 1, 1),
            ended_on=date(2001, 1, 1),
        )
    ]
    _, components = score_candidate(opportunity, candidate, stale, skills, today=TODAY)
    named = _by_name(components)
    assert named["tenure"].raw < Decimal(1)
    assert named["recency"].raw == Decimal(0)


async def test_tenure_and_recency_abstain_without_dated_roles() -> None:
    opportunity, candidate, _, skills = _full_case()
    undated = [
        FakeRole(
            title="Staff Nurse",
            employer="Acme Health",
            employer_normalized="acme health",
            started_on=None,
        )
    ]
    _, components = score_candidate(
        opportunity, candidate, undated, skills, today=TODAY
    )
    named = _by_name(components)
    for name in ("tenure", "recency"):
        assert named[name].raw is None
        assert named[name].note


async def test_no_component_reads_a_protected_characteristic() -> None:
    """Nothing may key off a birthday, so supplying one must change nothing."""
    opportunity, candidate, roles, skills = _full_case()
    before, _ = score_candidate(opportunity, candidate, roles, skills, today=TODAY)

    candidate.date_of_birth = date(1968, 3, 4)  # type: ignore[attr-defined]
    candidate.gender = "female"  # type: ignore[attr-defined]
    candidate.nationality = "Malaysian"  # type: ignore[attr-defined]
    candidate.marital_status = "married"  # type: ignore[attr-defined]

    after, _ = score_candidate(opportunity, candidate, roles, skills, today=TODAY)
    assert after == before
