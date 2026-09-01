"""Assemble the redacted opportunity context every stage's prompt reads.

Pure module — no database, no settings, no I/O. It takes an `Opportunity` (any
object exposing the fields below) and its `OpportunityCode` rows, and returns a
labelled block of text a prompt can interpolate.

The one rule this module exists to enforce is the rule the rest of the LLM
stack already enforces: **protected-attribute glossary codes never reach a
model.** Every free-text field the model is allowed to read goes through
`redact()` first — the same function `sourcing/embed.py` and `sourcing/explain.py`
call before their prompts do. A coded requirement (`C/F`, `O/F`, …) that reached
this engine would be reasoned about as if it were a job requirement, which is
the laundering `redact.py` exists to prevent.

Structured fields (salary numbers, period, duration months, employment type)
are passed through as-is: they are normalised columns, not recruiter prose, and
a code never reaches them. Only the `_raw` and free-text columns are redacted,
because those are where a code lives.
"""

from dataclasses import dataclass

from app.services.sourcing.redact import redact

# Fields whose text is recruiter prose and may carry a code. Each is redacted
# independently, because a code in the title and a code in the description are
# two different occurrences and must each be replaced.
# allow-hardcode: the column names of `opportunities`, not configuration.
_PROSE_FIELDS = (
    "job_title_raw",
    "job_description",
    "requirements",
    "working_hours_raw",
    "salary_raw",
    "duration_raw",
    "location_raw",
)


@dataclass(frozen=True)
class OpportunityContext:
    """The labelled, redacted text the prompts read, plus audit info.

    `removed_codes` is the list of codes `redact()` stripped, kept so the API
    can record (and a recruiter can later see) that a protected characteristic
    was present in the source and withheld from the analysis — the same audit
    property `SourcingRun.protected_attribute_note` records for sourcing.
    """

    text: str
    removed_codes: list[str]


def assemble(opportunity, codes=()) -> OpportunityContext:
    """Build the redacted opportunity block.

    Returns an empty-text context only when the opportunity carries no prose at
    all — a real job order always has at least a title or description, so an
    empty result is a signal the caller can treat as "nothing to analyse".
    """
    removed: list[str] = []
    lines: list[str] = []

    for field in _PROSE_FIELDS:
        raw = _clean(getattr(opportunity, field, None))
        if not raw:
            continue
        redacted, hits = redact(raw, list(codes or []))
        for code in hits:
            if code not in removed:
                removed.append(code)
        if redacted:
            lines.append(f"{_LABELS[field]}: {redacted}")

    # Structured fields: normalised columns a code cannot live in. Passed as
    # plain values, one per line, so the model reads them as facts about the
    # role rather than as more prose to interpret.
    structured = _structured_lines(opportunity)
    lines.extend(structured)

    text = "\n".join(lines)
    return OpportunityContext(text=text, removed_codes=removed)


def is_thin(opportunity) -> bool:
    """True when the order carries no description of the work itself.

    A title plus contract terms (salary, duration, employment type) is not
    enough for the understanding stage to reason about: the anti-fabrication
    rule in its prompt then makes the model refuse rather than invent, and a
    paid LLM call dies on `LLMInvalidJSON` (production, arq 2026-09-01 — a
    Business Operation Executive order whose only content was title, salary
    cap and a 1-year contract). The worker checks this *before* the pipeline
    and fails with an actionable sentence instead.

    The fields checked are exactly the ones that describe work: free-text
    description, requirements, and the skills array. A missing title does not
    make an order thin on its own — a description without a title is still
    analysable, and the empty-context case is already caught downstream.
    """
    has_description = _clean(getattr(opportunity, "job_description", None))
    has_requirements = _clean(getattr(opportunity, "requirements", None))
    skills = _clean_list(getattr(opportunity, "skills", None))
    return not (has_description or has_requirements or skills)


def _clean(value) -> str:
    return (value or "").strip()


def _clean_list(value) -> list[str]:
    return [v for v in (value or []) if _clean(v)]


def _structured_lines(opportunity) -> list[str]:
    """Salary, period, duration, employment type, work arrangement, skills.

    These are the columns the extraction pipeline already normalised, so they
    are trustworthy facts rather than prose to re-interpret. Skills is an
    ARRAY(Text); the rest are scalars.
    """
    out: list[str] = []

    salary = _salary_line(opportunity)
    if salary:
        out.append(salary)

    duration = _duration_line(opportunity)
    if duration:
        out.append(duration)

    for field, label in (
        ("employment_type", "Employment type"),
        ("work_arrangement", "Work arrangement"),
        ("industry", "Industry"),
    ):
        value = _clean(getattr(opportunity, field, None))
        if value:
            out.append(f"{label}: {value}")

    wanted = getattr(opportunity, "skills", None) or []
    skills_text = ", ".join(_clean(s) for s in wanted if _clean(s))
    if skills_text:
        out.append("Skills: " + skills_text)

    return out


def _salary_line(opportunity) -> str:
    minimum = getattr(opportunity, "salary_min", None)
    maximum = getattr(opportunity, "salary_max", None)
    currency = _clean(getattr(opportunity, "salary_currency", None))
    period = _clean(getattr(opportunity, "salary_period", None))
    if minimum is None and maximum is None:
        return ""
    parts: list[str] = []
    if currency:
        parts.append(currency)
    if minimum is not None and maximum is not None:
        parts.append(f"{_fmt(minimum)} – {_fmt(maximum)}")
    elif minimum is not None:
        parts.append(f"from {_fmt(minimum)}")
    else:
        parts.append(f"up to {_fmt(maximum)}")
    if period:
        parts.append(f"per {period}")
    return "Salary: " + " ".join(parts)


def _duration_line(opportunity) -> str:
    months = getattr(opportunity, "duration_months", None)
    if months is None:
        return ""
    if months == 1:
        return "Duration: 1 month"
    return f"Duration: {months} months"


def _fmt(value) -> str:
    """Drop trailing .00 from a Numeric so '3000.00' reads as '3000'."""
    if hasattr(value, "quantize"):
        # Decimal: strip trailing zeros without scientific notation.
        text = format(value.normalize(), "f")
        return text
    return str(value)


# Human-readable labels for the prose fields, named once so the prompt reads
# consistently. Keys are the opportunity column names.
# allow-hardcode: display labels for column names, not configuration.
_LABELS = {
    "job_title_raw": "Job title",
    "job_description": "Job description",
    "requirements": "Requirements",
    "working_hours_raw": "Working hours",
    "salary_raw": "Salary (as written)",
    "duration_raw": "Duration (as written)",
    "location_raw": "Location",
}
