"""Assemble the candidate context every stage's prompt reads.

Pure module — no database, no settings, no I/O. Takes a `Candidate`, its
`CandidateRole` rows, its `CandidateSkill` rows, and the parsed CV text fetched
from R2, and returns a labelled block of text a prompt can interpolate.

The one rule this module exists to enforce is the rule the rest of the LLM
stack already enforces (the same rule `sourcing/embed.py`'s
`candidate_text_for_embedding` applies): **protected attributes never reach a
model.** A candidate row carries sex, race, nationality and date of birth
because a MOM form asks for them; none of those may enter an analysis that
reasons about professional capability (design doc §22). So the structured facts
this module reads are a whitelist of job-related fields — title, employer,
years of experience — and the protected columns are never read at all.

The CV text is passed through unchanged. It is the source document the model is
reasoning about, and the prompt instructs the model to ignore any protected
attribute it encounters there for the purpose of deriving suitability. The
structured roles and skills are the parsed, verified output of the CV pipeline,
so they are trustworthy facts rather than prose to re-interpret.
"""

from dataclasses import dataclass, field

# The job-related structured fields on a candidate, named once so the whitelist
# is explicit and auditable. A column added to `Candidate` must not appear here
# by accident — anything identity- or demography-related is excluded on purpose.
# allow-hardcode: the column names of `candidates`, not configuration.
_CANDIDATE_FIELDS = (
    "current_title",
    "current_employer",
    "location",
    "years_experience",
    "expected_salary",
    "salary_currency",
    "salary_period",
    "employment_type",
    "available_from",
    "notice_period_raw",
)

# Human-readable labels for the structured fields. Keys are the candidate column
# names; named once so the prompt reads consistently.
# allow-hardcode: display labels for column names, not configuration.
_LABELS = {
    "current_title": "Current title",
    "current_employer": "Current employer",
    "location": "Location",
    "years_experience": "Years of experience",
    "expected_salary": "Expected salary",
    "salary_currency": "Salary currency",
    "salary_period": "Salary period",
    "employment_type": "Employment type",
    "available_from": "Available from",
    "notice_period_raw": "Notice period",
}

# The cap on CV text length. The model context limit is higher, but a very long
# CV dominates token cost and attention; the first N characters carry the career
# narrative. Applied here so a test asserts on untruncated text and the worker
# owns only the budget.
# allow-hardcode: a token/cost budget, not configuration.
_MAX_CV_CHARS = 12000


@dataclass(frozen=True)
class CandidateContext:
    """The labelled text the prompts read.

    `cv_text` is the parsed CV (possibly truncated); `structured` is the
    job-related facts drawn from the candidate row and its roles/skills. The two
    are kept separate so a prompt can present the CV as the primary source and
    the structured facts as verified context, rather than blending them.
    """

    cv_text: str
    structured: str
    # Whether the CV text was present at all. An empty CV (no parsed document)
    # is a signal the worker treats as "not enough to analyse"; a CV that
    # produced an empty analysis is a different condition. Keeping this flag
    # lets the worker distinguish the two without re-reading the document.
    has_cv: bool = field(default=True)


def assemble(candidate, roles=(), skills=(), cv_text: str = "") -> CandidateContext:
    """Build the candidate context block.

    `candidate` is a `Candidate`; `roles` its `CandidateRole` rows; `skills` its
    `CandidateSkill` rows; `cv_text` the parsed CV fetched from R2. Returns a
    context whose `has_cv` is False when no CV text was available, so the caller
    can fail with an actionable reason rather than analysing nothing.
    """
    structured_lines: list[str] = []

    for field_name in _CANDIDATE_FIELDS:
        value = _clean(getattr(candidate, field_name, None))
        if value:
            structured_lines.append(f"{_LABELS[field_name]}: {value}")

    role_lines = _role_lines(roles)
    structured_lines.extend(role_lines)

    skill_line = _skill_line(skills)
    if skill_line:
        structured_lines.append(skill_line)

    structured = "\n".join(structured_lines)
    truncated = _truncate(cv_text, _MAX_CV_CHARS)
    return CandidateContext(
        cv_text=truncated,
        structured=structured,
        has_cv=bool(truncated.strip()),
    )


def _role_lines(roles) -> list[str]:
    """The candidate's roles as labelled lines, oldest-first.

    Ordered by `started_on` so the model reads a chronological career rather
    than an insertion-order list. Rejected roles are skipped — a human said they
    did not happen — mirroring `sourcing/embed.py`'s rule.
    """
    if not roles:
        return []
    lines: list[str] = []

    def _sort_key(role):
        # `started_on` is a date from the DB, but a role without one yields
        # None (and `or ""` would then compare a str against a date — a
        # TypeError). Normalise every value to an ISO string first so the
        # comparison is always str-vs-str, and put missing dates first
        # (empty string sorts before any date).
        started = getattr(role, "started_on", None)
        return started.isoformat() if started is not None else ""

    ordered = sorted(
        (r for r in roles if getattr(r, "status", None) != "rejected"),
        key=_sort_key,
    )
    for role in ordered:
        title = _clean(getattr(role, "title", None))
        employer = _clean(getattr(role, "employer", None))
        period = _role_period(role)
        description = _clean(getattr(role, "description", None))
        parts: list[str] = []
        if title:
            parts.append(title)
        if employer:
            parts.append(f"at {employer}")
        if period:
            parts.append(f"({period})")
        if parts:
            line = "  - " + " ".join(parts)
            if description:
                line += f": {description}"
            lines.append(line)
    return lines


def _role_period(role) -> str:
    """Render a role's start–end as a free-form period string.

    Uses the raw dates without forcing a precision the source did not give; a CV
    that says "2019" stays "2019", not "2019-01-01".
    """
    start = getattr(role, "started_on", None)
    end = getattr(role, "ended_on", None)
    if start and end:
        return f"{start} to {end}"
    if start:
        return f"{start} to present"
    if end:
        return f"until {end}"
    return ""


def _skill_line(skills) -> str:
    """The candidate's skills as one labelled, de-duplicated line."""
    held: list[str] = []
    for skill in skills or ():
        name = _clean(
            getattr(skill, "skill_normalized", None) or getattr(skill, "skill", None)
        )
        if name and name not in held:
            held.append(name)
    if not held:
        return ""
    return "Skills: " + ", ".join(held)


def _clean(value) -> str:
    """Strip and drop None, so absent fields contribute nothing.

    Coerces non-strings (`years_experience` is an int, `expected_salary` a
    Decimal) to their `str` form before stripping, so a numeric field reads as
    a value rather than failing the `.strip()` call.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _truncate(text: str, max_chars: int) -> str:
    """Cap CV text at `max_chars` on a character boundary.

    A character-boundary cut is blunt but deterministic — the same budget every
    run, independent of where a word or sentence boundary falls.
    """
    if not text or max_chars <= 0:
        return ""
    return text[:max_chars]
