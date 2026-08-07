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
    AutomationAssessment,
    CandidateIntelligenceResult,
    HistoryRole,
    WorkItem,
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


def test_schema_has_five_stages():
    schema = json_schema()
    assert set(schema.keys()) == {"history", "automation", "benchmark", "gaps", "residual"}


def test_schema_history_has_nested_roles_with_work():
    """A history role carries a nested work-item list (the L2 decomposition)."""
    role_items = json_schema()["history"]["properties"]["roles"]["items"]
    assert role_items["type"] == "object"
    assert "work" in role_items["properties"]
    work_items = role_items["properties"]["work"]["items"]
    assert set(work_items["properties"].keys()) == {
        "task",
        "tool",
        "judgment_level",
        "accountability",
    }


def test_schema_automation_entry_has_required_prose():
    """Every automation assessment must carry a reason + residual value (guardrails 5/6)."""
    entry_props = (
        json_schema()["automation"]["properties"]["assessments"]["items"]["properties"]
    )
    assert set(entry_props.keys()) == {
        "capability",
        "automation_level",
        "automation_reason",
        "residual_human_value",
    }


def test_result_model_round_trips():
    """The aggregate result model validates a full five-stage payload."""
    # allow-hardcode: test fixture content, not an oracle.
    result = CandidateIntelligenceResult.model_validate(
        {
            "history": {
                "roles": [
                    {
                        "period": "2019",
                        "title": "Analyst",
                        "domain": "Banking",
                        "seniority": "junior",
                        "scope": "",
                        "work": [
                            {
                                "task": "Reconciliation",
                                "tool": "Excel",
                                "judgment_level": "routine",
                                "accountability": "",
                            }
                        ],
                        "evidence": "did reconciliation",
                    }
                ],
                "industries": ["Banking"],
                "functions": ["Analysis"],
                "systems": ["Excel"],
                "trajectory": ["Banking"],
            },
            "automation": {
                "assessments": [
                    {
                        "capability": "Reconciliation",
                        "automation_level": "high",
                        "automation_reason": "Rules engines handle standard recon.",
                        "residual_human_value": "Exception investigation",
                    }
                ],
                "scarce_capabilities": ["Exception investigation"],
            },
            "benchmark": {
                "work_family": "Banking operations",
                "current_work": ["Automated reconciliation"],
                "current_required": ["Exception handling"],
                "declining": ["Manual reconciliation"],
                "emerging": ["Automation oversight"],
                "scarce": ["Exception judgment"],
                "automation_summary": "Largely automated.",
            },
            "gaps": {
                "gaps": [
                    {
                        "capability": "Automation oversight",
                        "status": "not_evidenced",
                        "note": "Not shown in CV.",
                    }
                ],
                "evidence_gaps": ["Oversight scope"],
            },
            "residual": {
                "historical_strength": "Banking ops",
                "automation_exposure": "High",
                "current_relevance": "Exceptions only",
                "scarce_capabilities": ["Exception judgment"],
                "depreciated_capabilities": ["Manual reconciliation"],
                "emerging_capabilities": [],
                "evidence_gaps": ["Oversight"],
                "overall_assessment": "Routine work automated.",
                "current_profile": "Ops professional.",
            },
        }
    )
    assert isinstance(result.history.roles[0], HistoryRole)
    assert isinstance(result.history.roles[0].work[0], WorkItem)
    assert isinstance(result.automation.assessments[0], AutomationAssessment)
    assert result.history.roles[0].title == "Analyst"
