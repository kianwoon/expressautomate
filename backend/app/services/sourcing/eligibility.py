"""Whether a candidate may be legitimately narrowed for a job order (§15).

Pure module — no database, no I/O — the same shape as `redact.py`. It takes a
placement type and a candidate's recorded facts and returns findings; nothing
here decides what a caller does with them, and nothing here collapses the
result to a boolean. A single `eligible: true/false` field would destroy the
two outcomes (`unknown`, `not_applicable`) this slice exists to make visible,
so this module offers no such summary and none should ever be added — a
caller that wants one has to build it from the findings, in the open, where a
reviewer can see what got collapsed.

Three bases keep this module honest about what it is allowed to do, and the
comment lives here because a reader who only sees the code could confuse them:

- **Regulatory eligibility** (`BASIS_REGULATORY`) — MOM sets it. A Migrant
  Domestic Worker (MDW) Work Permit requires the holder be female, aged
  `min_age_years` to under `max_age_years_exclusive` **at application**, with
  at least `min_education_years` years of formal education, from a country in
  `approved_source_countries`. Applying these is compliance.
- **Genuine occupational requirement** (`BASIS_OCCUPATIONAL`) — the job
  itself imposes a sex requirement and a recruiter states why
  (`Opportunity.sex_requirement` / `.sex_requirement_reason`). This module
  reports it as a finding, with the recruiter's reason carried into `detail`
  **verbatim** — that reason is the justification, and paraphrasing it would
  weaken the record it exists to be. This module does not judge whether the
  stated reason is actually genuine; that is a human call the law leaves to a
  person, not a database CHECK.
- **Client preference** — the `C/F`-style shorthand a glossary decodes.
  Recorded as evidence, flagged, redacted (`redact.py`). Never acted on, and
  this module never reads it: nothing about a client's coded preference is a
  finding here.

Four outcomes, not two, because a candidate can fail a rule, fail to be
assessable against it, or simply not be governed by it at all — three
different sentences a recruiter needs told apart:

- `met` / `not_met` — the rule applies and we hold the fact to judge it.
- `unknown` — the rule applies and we hold no fact to judge it. A candidate
  with no `date_of_birth` is not ineligible on age, they are unassessed;
  collapsing this into `not_met` would hide a data gap behind a
  legal-sounding word, which is exactly the fabrication §15 forbids.
- `not_applicable` — the rule does not govern this placement type at all. A
  local hire has no MOM age band to fail *or* to be missing a date of birth
  for; forcing that into `unknown` would send a recruiter hunting for a fact
  nothing requires.

Every criterion appears on every call, in a fixed order — a rule that does
not apply says so rather than being left out, and a caller never has to
re-sort what it renders.

Age is computed at evaluation time from `date_of_birth`, as of a caller-given
date, and never stored — a stored age rots the moment a year passes and
nothing would say when it was true. MOM judges age at *application*, not at
whatever day someone happens to look, so `evaluate()` takes `as_of` rather
than assuming "today"; near a boundary, the age finding also says *when* the
answer changes, because "aged 49" and "aged 49, turning 50 on 3 March 2027"
are different amounts of information for a recruiter planning ahead.

`detail` on the nationality finding describes *this system's configuration*,
never MOM's actual policy — "is in this system's configured approved-source
list (checked 2026-07-30)", not "MOM approves the Philippines". The list is a
setting that can go stale; this module can vouch for what it was configured
with, not for what the regulator's list says today.
"""

from dataclasses import dataclass
from datetime import date, timedelta

MET = "met"
NOT_MET = "not_met"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
OUTCOMES = (MET, NOT_MET, UNKNOWN, NOT_APPLICABLE)

BASIS_REGULATORY = "regulatory"
BASIS_OCCUPATIONAL = "occupational"
BASES = (BASIS_REGULATORY, BASIS_OCCUPATIONAL)

CRITERION_SEX = "sex"
CRITERION_AGE = "age"
CRITERION_EDUCATION = "education"
CRITERION_NATIONALITY = "nationality"
CRITERION_OCCUPATIONAL_SEX_REQUIREMENT = "occupational_sex_requirement"

MDW_WORK_PERMIT = "mdw_work_permit"

# Stable, and deliberately fixed: the API contract promises the same order on
# every call so a caller (the UI) never has to re-sort what it renders.
_MDW_CRITERIA_ORDER = (
    CRITERION_SEX,
    CRITERION_AGE,
    CRITERION_EDUCATION,
    CRITERION_NATIONALITY,
)

# allow-hardcode: human-authored sentence fragments for a `detail` string, not
# a matching or scoring oracle. `Opportunity.PLACEMENT_TYPES` is the actual
# vocabulary (enforced by the model and the database CHECK); this dict only
# supplies the words a recruiter reads when a criterion does not apply, and a
# type absent from it still gets a readable message via `.get(..., placement_type)`.
_PLACEMENT_TYPE_LABELS = {
    "local_hire": "a local hire",
    "mdw_work_permit": "a MDW Work Permit placement",
    "other_work_permit": "another Work Permit type",
    "s_pass": "an S Pass placement",
    "employment_pass": "an Employment Pass placement",
}

# Near a boundary, the age finding says when the answer changes. This is how
# near counts as near — inside a year, a recruiter planning a placement cares;
# beyond it, the date is noise next to the current answer.
_BOUNDARY_LOOKAHEAD_DAYS = 365


@dataclass(frozen=True)
class CandidateFacts:
    """The subset of `Candidate` this module needs, so it never imports the
    ORM model — the same reason `redact.py` accepts anything exposing `code`
    and `attribute` rather than importing `OpportunityCode`."""

    sex: str | None
    date_of_birth: date | None
    education_years: int | None
    nationality: str | None


@dataclass(frozen=True)
class Finding:
    criterion: str
    outcome: str
    detail: str
    basis: str


def _age_years(date_of_birth: date, as_of: date) -> int:
    years = as_of.year - date_of_birth.year
    if (as_of.month, as_of.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


def _birthday_turning(date_of_birth: date, target_age: int) -> date:
    """The date `date_of_birth` turns `target_age`. Naive Feb-29 handling
    (falls back to Mar 1) is acceptable here: this is advisory prose about
    when an answer changes, not the CHECK constraint that governs a write."""
    try:
        return date_of_birth.replace(year=date_of_birth.year + target_age)
    except ValueError:
        return date_of_birth.replace(year=date_of_birth.year + target_age, day=28) + timedelta(
            days=1
        )


def _boundary_note(turning_date: date, as_of: date, target_age: int, effect: str) -> str:
    if 0 <= (turning_date - as_of).days <= _BOUNDARY_LOOKAHEAD_DAYS:
        return f" Turns {target_age} on {turning_date.isoformat()}, {effect}."
    return ""


def _not_applicable(criterion: str, placement_type: str, rule_name: str) -> Finding:
    label = _PLACEMENT_TYPE_LABELS.get(placement_type, placement_type)
    return Finding(
        criterion,
        NOT_APPLICABLE,
        f"This placement is {label}; {rule_name} does not apply.",
        BASIS_REGULATORY,
    )


def _sex_finding(placement_type: str, facts: CandidateFacts) -> Finding:
    if placement_type != MDW_WORK_PERMIT:
        return _not_applicable(CRITERION_SEX, placement_type, "the permit's sex requirement")
    if facts.sex is None:
        return Finding(CRITERION_SEX, UNKNOWN, "No sex recorded.", BASIS_REGULATORY)
    if facts.sex == "female":
        return Finding(CRITERION_SEX, MET, "Recorded as female.", BASIS_REGULATORY)
    return Finding(
        CRITERION_SEX,
        NOT_MET,
        "Recorded as male; the permit requires female.",
        BASIS_REGULATORY,
    )


def _age_finding(
    placement_type: str,
    facts: CandidateFacts,
    as_of: date,
    min_age_years: int,
    max_age_years_exclusive: int,
) -> Finding:
    if placement_type != MDW_WORK_PERMIT:
        return _not_applicable(CRITERION_AGE, placement_type, "the permit age range")
    if facts.date_of_birth is None:
        return Finding(CRITERION_AGE, UNKNOWN, "No date of birth recorded.", BASIS_REGULATORY)

    age = _age_years(facts.date_of_birth, as_of)
    if min_age_years <= age < max_age_years_exclusive:
        note = _boundary_note(
            _birthday_turning(facts.date_of_birth, max_age_years_exclusive),
            as_of,
            max_age_years_exclusive,
            "when the permit's age range no longer applies",
        )
        return Finding(
            CRITERION_AGE, MET, f"Aged {age}, within the permitted range.{note}", BASIS_REGULATORY
        )

    if age < min_age_years:
        note = _boundary_note(
            _birthday_turning(facts.date_of_birth, min_age_years),
            as_of,
            min_age_years,
            "when the permit's age range begins",
        )
        detail = (
            f"Aged {age}; the permit requires {min_age_years} to under "
            f"{max_age_years_exclusive}.{note}"
        )
    else:
        detail = (
            f"Aged {age}; the permit requires {min_age_years} to under "
            f"{max_age_years_exclusive}."
        )
    return Finding(CRITERION_AGE, NOT_MET, detail, BASIS_REGULATORY)


def _education_finding(
    placement_type: str, facts: CandidateFacts, min_education_years: int
) -> Finding:
    if placement_type != MDW_WORK_PERMIT:
        return _not_applicable(
            CRITERION_EDUCATION, placement_type, "the permit's education minimum"
        )
    if facts.education_years is None:
        return Finding(
            CRITERION_EDUCATION, UNKNOWN, "No years of education recorded.", BASIS_REGULATORY
        )
    if facts.education_years >= min_education_years:
        return Finding(
            CRITERION_EDUCATION,
            MET,
            f"{facts.education_years} years recorded; meets the "
            f"{min_education_years}-year minimum.",
            BASIS_REGULATORY,
        )
    return Finding(
        CRITERION_EDUCATION,
        NOT_MET,
        f"{facts.education_years} years recorded; the permit requires at least "
        f"{min_education_years}.",
        BASIS_REGULATORY,
    )


def _nationality_finding(
    placement_type: str,
    facts: CandidateFacts,
    as_of: date,
    approved_source_countries: frozenset[str],
) -> Finding:
    if placement_type != MDW_WORK_PERMIT:
        return _not_applicable(
            CRITERION_NATIONALITY, placement_type, "the permit's source-country list"
        )
    if facts.nationality is None:
        return Finding(
            CRITERION_NATIONALITY, UNKNOWN, "No nationality recorded.", BASIS_REGULATORY
        )
    checked = f"(checked {as_of.isoformat()})"
    if facts.nationality.upper() in approved_source_countries:
        return Finding(
            CRITERION_NATIONALITY,
            MET,
            f"{facts.nationality} is in this system's configured approved-source list "
            f"{checked}.",
            BASIS_REGULATORY,
        )
    return Finding(
        CRITERION_NATIONALITY,
        NOT_MET,
        f"{facts.nationality} is not in this system's configured approved-source list "
        f"{checked}.",
        BASIS_REGULATORY,
    )


def _occupational_sex_requirement_finding(
    facts: CandidateFacts, sex_requirement: str | None, sex_requirement_reason: str | None
) -> Finding:
    """Not regulatory. Reports the job's own stated requirement, with the
    recruiter's reason carried into `detail` verbatim — never `unknown`,
    because the job either states a requirement or it does not; there is no
    missing fact to be unassessed about."""
    if sex_requirement is None:
        return Finding(
            CRITERION_OCCUPATIONAL_SEX_REQUIREMENT,
            MET,
            "The job order states no sex requirement.",
            BASIS_OCCUPATIONAL,
        )
    if facts.sex is None:
        return Finding(
            CRITERION_OCCUPATIONAL_SEX_REQUIREMENT,
            UNKNOWN,
            f"No sex recorded for the candidate; the job requires {sex_requirement} — "
            f"{sex_requirement_reason}",
            BASIS_OCCUPATIONAL,
        )
    if facts.sex == sex_requirement:
        return Finding(
            CRITERION_OCCUPATIONAL_SEX_REQUIREMENT,
            MET,
            f"Matches the job's stated requirement — {sex_requirement_reason}",
            BASIS_OCCUPATIONAL,
        )
    return Finding(
        CRITERION_OCCUPATIONAL_SEX_REQUIREMENT,
        NOT_MET,
        f"Does not match the job's stated requirement of {sex_requirement} — "
        f"{sex_requirement_reason}",
        BASIS_OCCUPATIONAL,
    )


def evaluate(
    placement_type: str | None,
    facts: CandidateFacts,
    *,
    as_of: date,
    min_age_years: int,
    max_age_years_exclusive: int,
    min_education_years: int,
    approved_source_countries: frozenset[str],
    sex_requirement: str | None = None,
    sex_requirement_reason: str | None = None,
) -> list[Finding]:
    """Findings for one candidate against one job order, as of `as_of`.

    No boolean summary is returned, and none should be added: an
    `eligible: true/false` field would collapse `unknown` and
    `not_applicable` into whichever bucket a caller chose, which destroys the
    two distinctions this module exists to preserve. A caller that wants a
    yes/no answer builds it from `findings`, in view of whoever reads the
    code, rather than getting one handed to it pre-collapsed.

    Every regulatory bound is a parameter, never a module-level constant or a
    `settings` import — the caller (the API layer) reads `settings` and
    passes the values in, keeping this module pure and testable against a
    policy that has already changed once and will again.

    `placement_type=None` is the caller's problem to handle (a job order not
    yet classified), not this function's — call it only once a placement
    type is known.

    A non-MDW placement type still returns every regulatory criterion, each
    `not_applicable`: MOM's age, education, sex and source-country rules
    simply do not govern a local hire or a non-MDW work pass, and a criterion
    that does not apply says so rather than being omitted.
    """
    findings = {
        CRITERION_SEX: _sex_finding(placement_type, facts),
        CRITERION_AGE: _age_finding(
            placement_type, facts, as_of, min_age_years, max_age_years_exclusive
        ),
        CRITERION_EDUCATION: _education_finding(placement_type, facts, min_education_years),
        CRITERION_NATIONALITY: _nationality_finding(
            placement_type, facts, as_of, approved_source_countries
        ),
    }
    ordered = [findings[criterion] for criterion in _MDW_CRITERIA_ORDER]
    ordered.append(
        _occupational_sex_requirement_finding(facts, sex_requirement, sex_requirement_reason)
    )
    return ordered
