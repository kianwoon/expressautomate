"""A protected-attribute code never reaches the Job Intelligence model.

This is the test the whole redaction discipline exists to backstop. A job order
whose title or description carries a coded protected characteristic (e.g. `C/F`,
decoded by the glossary to "Chinese Female") must have that code stripped to
`[redacted]` before any of the three prompts see the text. The laundering this
prevents — reasoning about a candidate on race or gender — is the one thing the
glossary/redact stack exists to prevent, and this layer inherits that rule.

It mirrors `test_sourcing_match_redaction.py` for the sourcing stack: the same
protection, applied at the new boundary where the Job Intelligence engine reads
opportunity text.

allow-hardcode: the code and opportunity text below are test fixtures.
"""

from dataclasses import dataclass

from app.services.job_intelligence.input import assemble


@dataclass
class _Code:
    """The attributes `redact()` reads from an `OpportunityCode` row."""

    code: str
    attribute: str | None


@dataclass
class _Opp:
    job_title_raw: str
    job_description: str
    requirements: str = ""
    working_hours_raw: str = ""
    salary_raw: str = ""
    duration_raw: str = ""
    location_raw: str = ""
    salary_min: object = None
    salary_max: object = None
    salary_currency: str = ""
    salary_period: str = ""
    duration_months: object = None
    employment_type: str = ""
    work_arrangement: str = ""
    industry: str = ""
    skills: list = None


def test_protected_code_is_redacted_from_the_context():
    """A sex/race code is replaced with [redacted], not handed to the model."""
    opp = _Opp(
        job_title_raw="Operations Executive C/F",
        job_description="Hire a C/F for the day shift. Must be reliable.",
    )
    codes = [_Code(code="C/F", attribute="sex")]
    context = assemble(opp, codes)
    # The verbatim code is gone from the text the prompts would read.
    assert "C/F" not in context.text
    # It was replaced with the marker, not spliced out.
    assert "[redacted]" in context.text
    # And it is reported for audit.
    assert context.removed_codes == ["C/F"]


def test_non_attribute_code_is_preserved():
    """A code with no `attribute` (e.g. a shift code) is a real requirement."""
    opp = _Opp(
        job_title_raw="Driver",
        job_description="Must accept NS shift.",
    )
    # `NS` with no attribute is a shift requirement, not protected.
    codes = [_Code(code="NS", attribute=None)]
    context = assemble(opp, codes)
    assert "NS" in context.text
    assert context.removed_codes == []


def test_no_codes_leaves_text_untouched():
    opp = _Opp(job_title_raw="Chef", job_description="Cook for the canteen.")
    context = assemble(opp, codes=())
    assert context.text.splitlines()[0] == "Job title: Chef"
    assert context.removed_codes == []


def test_structured_fields_are_not_redacted():
    """Normalised columns (salary, period) pass through as facts."""
    opp = _Opp(
        job_title_raw="Logistics Manager",
        job_description="Run the warehouse.",
        salary_min=3000,
        salary_max=5000,
        salary_currency="SGD",
        salary_period="month",
        duration_months=6,
        employment_type="Contract",
        skills=["SAP", "Excel"],
    )
    context = assemble(opp, codes=())
    assert "Salary: SGD 3000 – 5000 per month" in context.text
    assert "Duration: 6 months" in context.text
    assert "Employment type: Contract" in context.text
    assert "Skills: SAP, Excel" in context.text
