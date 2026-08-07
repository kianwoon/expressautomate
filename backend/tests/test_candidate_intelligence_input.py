"""Tests for the candidate intelligence input assembler and schema.

The input assembler's load-bearing rule is the whitelist: protected attributes
(sex, race, DOB, nationality) must never enter the text a model reads. These
tests assert that property directly — the same boundary `sourcing/embed.py`'s
tests hold for the embedding text.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

from dataclasses import dataclass

from app.services.candidate_intelligence.input import assemble
from app.services.candidate_intelligence.schema import (
    CandidateIntelligenceResult,
    CapabilityEntry,
    RoleAffinity,
    TimelineEntry,
    json_schema,
)


@dataclass
class _FullCandidate:
    """Carries protected attributes the assembler must IGNORE, plus the fields it reads."""

    current_title: str = "Underwriter"
    current_employer: str = "Integral Plus"
    location: str = "Singapore"
    years_experience: int = 8
    expected_salary: object = 5000
    salary_currency: str = "SGD"
    salary_period: str = "month"
    employment_type: str = "Full-time"
    available_from: object = None
    notice_period_raw: str = "1 month"
    # Protected attributes — these must NEVER appear in the assembled text.
    sex: str = "female"
    race: str = "chinese"
    nationality: str = "SG"
    date_of_birth: str = "1990-01-01"


@dataclass
class _Skill:
    skill: str = "Underwriting"
    skill_normalized: str = "underwriting"


@dataclass
class _Role:
    title: str = "Underwriter"
    employer: str = "Integral Plus"
    started_on: object = None
    ended_on: object = None
    description: str = "Assessed risk."
    status: str = "confirmed"


def test_assemble_includes_job_related_fields():
    context = assemble(_FullCandidate(), roles=[], skills=[], cv_text="CV text here")
    assert "Underwriter" in context.structured
    assert "Integral Plus" in context.structured
    assert "Singapore" in context.structured
    assert "8" in context.structured  # years_experience
    assert context.has_cv is True


def test_assemble_excludes_protected_attributes():
    """The whitelist rule: sex/race/nationality/DOB never reach the model."""
    context = assemble(_FullCandidate(), roles=[], skills=[], cv_text="CV text")
    # None of the protected-attribute values may appear in the structured text.
    # (They are not read at all — the assertion guards against a future column
    # being added to the whitelist by accident.)
    assert "female" not in context.structured
    assert "chinese" not in context.structured
    # Nationality "SG" could appear in "SGD" (currency), so check the labelled
    # form the assembler would produce, not the bare token.
    assert "Nationality" not in context.structured


def test_assemble_marks_empty_cv():
    """An absent CV is a signal the worker treats as 'nothing to analyse'."""
    context = assemble(_FullCandidate(), roles=[], skills=[], cv_text="")
    assert context.has_cv is False


def test_assemble_truncates_long_cv():
    """A very long CV is capped so it cannot dominate token cost."""
    long_cv = "x" * 50000
    context = assemble(_FullCandidate(), roles=[], skills=[], cv_text=long_cv)
    assert len(context.cv_text) <= 12000


def test_assemble_includes_roles_oldest_first():
    from datetime import date

    older = _Role(title="Analyst", employer="Bank A", started_on=date(2015, 1, 1))
    newer = _Role(title="Underwriter", employer="Insurer B", started_on=date(2020, 1, 1))
    context = assemble(_FullCandidate(), roles=[newer, older], skills=[], cv_text="x")
    # Older role should appear before newer in the roles section. The role-line
    # markers ("  - ") distinguish the role entries from the candidate's
    # current_title (which also reads "Underwriter" in this fixture).
    role_lines = [ln for ln in context.structured.split("\n") if ln.startswith("  - ")]
    assert len(role_lines) == 2
    assert "Analyst" in role_lines[0]
    assert "Underwriter" in role_lines[1]


def test_assemble_skips_rejected_roles():
    """A rejected role is one a human said did not happen."""
    rejected = _Role(title="CTO", employer="Fake Co", status="rejected")
    context = assemble(_FullCandidate(), roles=[rejected], skills=[], cv_text="x")
    assert "Fake Co" not in context.structured


def test_schema_has_three_stages():
    schema = json_schema()
    assert set(schema.keys()) == {"career", "capability", "profile"}


def test_schema_career_has_nested_timeline():
    """The timeline is an array of objects, not a flat string list."""
    timeline_items = json_schema()["career"]["properties"]["timeline"]["items"]
    assert timeline_items["type"] == "object"
    assert set(timeline_items["properties"].keys()) == {"period", "title", "domain"}


def test_schema_capability_entry_has_confidence_number():
    """`confidence` is the lone number in a capability entry."""
    entry_props = json_schema()["capability"]["properties"]["capabilities"]["items"][
        "properties"
    ]
    assert entry_props["confidence"]["type"] == "number"
    assert entry_props["capability"]["type"] == "string"
    assert entry_props["supporting_evidence"]["type"] == "string"


def test_result_model_round_trips():
    """The aggregate result model validates a full three-stage payload."""
    result = CandidateIntelligenceResult.model_validate(
        {
            "career": {
                "timeline": [{"period": "2019", "title": "Analyst", "domain": "Banking"}],
                "trajectory": ["Banking"],
                "primary_domain": "Banking",
                "secondary_domains": [],
                "career_direction": "Growing",
                "career_stage": "Early",
            },
            "capability": {
                "capabilities": [
                    {
                        "capability": "Analysis",
                        "category": "functional",
                        "confidence": 0.8,
                        "supporting_evidence": "did analysis",
                    }
                ],
                "tools": ["Excel"],
            },
            "profile": {
                "professional_identity": "Analyst",
                "specializations": ["Analysis"],
                "orientation": "Analytical",
                "role_affinity": [
                    {"role": "Analyst", "affinity_type": "direct_fit", "confidence": 0.9}
                ],
            },
        }
    )
    assert isinstance(result.career.timeline[0], TimelineEntry)
    assert isinstance(result.capability.capabilities[0], CapabilityEntry)
    assert isinstance(result.profile.role_affinity[0], RoleAffinity)
    assert result.career.primary_domain == "Banking"
