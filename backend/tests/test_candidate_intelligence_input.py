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
    WorkUnit,
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


def test_assemble_sorts_roles_with_missing_dates():
    """A role with no started_on (None) must not crash the sort.

    The sort key was `started_on or ""`, which compared a datetime.date
    against a str when one role lacked a date — TypeError in production
    (run_candidate_intelligence failed on '<' not supported between
    'str' and 'datetime.date'). Missing dates sort first (empty string).
    """
    from datetime import date

    dated = _Role(title="Dated", employer="Bank A", started_on=date(2020, 1, 1))
    undated = _Role(title="Undated", employer="Bank B", started_on=None)
    context = assemble(_FullCandidate(), roles=[dated, undated], skills=[], cv_text="x")
    role_lines = [ln for ln in context.structured.split("\n") if ln.startswith("  - ")]
    assert len(role_lines) == 2
    # The undated role sorts first (its key is ""), the dated one after.
    assert "Undated" in role_lines[0]
    assert "Dated" in role_lines[1]


def test_assemble_skips_rejected_roles():
    """A rejected role is one a human said did not happen."""
    rejected = _Role(title="CTO", employer="Fake Co", status="rejected")
    context = assemble(_FullCandidate(), roles=[rejected], skills=[], cv_text="x")
    assert "Fake Co" not in context.structured


def test_schema_has_two_stages():
    schema = json_schema()
    assert set(schema.keys()) == {"work", "assessment"}


def test_schema_work_unit_has_decision_ownership_and_inflation():
    """A work unit carries decision_ownership (0-5) and an inflated flag."""
    role_items = json_schema()["work"]["properties"]["roles"]["items"]
    work_items = role_items["properties"]["work_units"]["items"]
    props = set(work_items["properties"].keys())
    assert "decision_ownership" in props
    assert "inflated" in props
    assert "ai_heavy_lift" in props
    assert work_items["properties"]["inflated"]["type"] == "boolean"


def test_result_model_round_trips():
    """The result model validates a full two-stage payload."""
    # allow-hardcode: test fixture content, not an oracle.
    result = CandidateIntelligenceResult.model_validate(
        {
            "work": {
                "roles": [
                    {
                        "employer": "Insurer",
                        "period": "2019-2023",
                        "stated_title": "Senior Executive",
                        "industry": "Insurance",
                        "work_units": [
                            {
                                "claim": "Managed portfolio",
                                "work": "Applied rules to submissions",
                                "decision_ownership": "2",
                                "complexity": "operational",
                                "ai_heavy_lift": "ai_heavy_lift",
                                "human_residual": "Exception review",
                                "evidence": "C",
                                "evidence_note": "Inflated.",
                                "inflated": True,
                            }
                        ],
                        "contribution_maturity": "independent",
                        "tenure_months": 42,
                    }
                ],
                "education": [],
            },
            "assessment": {
                "headline": "Routine operator.",
                "summary": "Operational work.",
                "work_level": "operational",
                "decision_authority": "Level 2.",
                "scarce_capabilities": [],
                "depreciated_capabilities": [
                    {"capability": "Processing", "reason": "Automated."}
                ],
                "unproven_claims": [
                    {"claim": "Managed", "question": "What did you own?"}
                ],
                "ai_exposure": "High.",
                "hire_readiness": "Immediate.",
                "value_trajectory": "Declining.",
            },
        }
    )
    assert isinstance(result.work.roles[0].work_units[0], WorkUnit)
    assert result.work.roles[0].work_units[0].inflated is True
    assert result.assessment.work_level == "operational"
