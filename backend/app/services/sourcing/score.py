"""How well a candidate fits a job order, and what the answer is based on.

Pure arithmetic over rows somebody else loaded, so it can be read and tested
without a database. It produces a total *and* the components behind it,
because a number a recruiter cannot interrogate is a number they will not act
on.

Two rules shape everything here.

**Absent is not bad.** A component with nothing to compare reports `raw=None`
and a note saying so, and is left out of the total entirely. Scoring it zero
would say "this person is a poor fit on salary" when the truth is that nobody
recorded a salary. The total is therefore a weighted *mean over the components
that had data* — dividing by the full weight set would mark a sparse record
down for the sparseness rather than for the fit, and early records are almost
all sparse.

**Knowing nothing is not a score.** When no component has data, there is no
total: this returns `None` and the caller drops the candidate with a note. A
`0.0` in that position reads as "unsuitable", and the difference between the
two is the credibility of the whole feature.

Nothing here reads, infers or approximates a protected characteristic. Years
of experience stay because they are job-related and stated by the candidate;
no component derives an age, and none ever should — see `SourcingRun.
protected_attribute_noticed` for where a discriminatory *requirement* is
recorded instead of being quietly obeyed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.config import settings
from app.services.candidate_naming import normalize_skill
from app.services.candidate_tenure import derive, span_months, union_months
from app.services.client_naming import normalize_company_name
from app.services.sourcing.text import overlap, salary_fit

TITLE = "title"
SKILLS = "skills"
EMPLOYER = "employer"
SALARY = "salary"
TENURE = "tenure"
RECENCY = "recency"

# The order the breakdown is rendered in: what the job asked for first, then
# what the career shape says. Fixed so two runs over the same candidate
# produce byte-identical output.
COMPONENT_NAMES = (TITLE, SKILLS, EMPLOYER, SALARY, TENURE, RECENCY)

_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass(frozen=True)
class Component:
    """One named signal's contribution to a score.

    `raw` is the signal itself in [0, 1]; `contribution` is `weight × raw`.
    Both are `None` together when there was nothing to compare, and `note`
    then says what was missing. A note may also accompany a real score to
    explain it.
    """

    name: str
    weight: Decimal
    raw: Decimal | None
    contribution: Decimal | None
    note: str | None = None

    @property
    def has_data(self) -> bool:
        return self.raw is not None


def default_weights() -> dict[str, Decimal]:
    """The configured weight of each component.

    Read on every call rather than captured at import, so an operator's change
    takes effect on the next run instead of the next deploy.
    """
    return {
        TITLE: _decimal(settings.SOURCING_WEIGHT_TITLE),
        SKILLS: _decimal(settings.SOURCING_WEIGHT_SKILLS),
        EMPLOYER: _decimal(settings.SOURCING_WEIGHT_EMPLOYER),
        SALARY: _decimal(settings.SOURCING_WEIGHT_SALARY),
        TENURE: _decimal(settings.SOURCING_WEIGHT_TENURE),
        RECENCY: _decimal(settings.SOURCING_WEIGHT_RECENCY),
    }


def _decimal(value: float | int) -> Decimal:
    """Via `str`, so 0.1 is a tenth rather than the binary float near it."""
    return Decimal(str(value))


def _quantum() -> Decimal:
    return _ONE.scaleb(-settings.SOURCING_SCORE_DECIMAL_PLACES)


def _round(value: Decimal) -> Decimal:
    return value.quantize(_quantum(), rounding=ROUND_HALF_UP)


def _clamp(value: Decimal) -> Decimal:
    """Into [0, 1]. The bounds are the definition of a raw signal, not a knob."""
    return min(max(value, _ZERO), _ONE)


def _scored(name: str, weight: Decimal, raw: Decimal, note: str | None = None) -> Component:
    value = _round(_clamp(raw))
    return Component(name=name, weight=weight, raw=value, contribution=weight * value, note=note)


def _absent(name: str, weight: Decimal, note: str) -> Component:
    return Component(name=name, weight=weight, raw=None, contribution=None, note=note)


def _job_title(opportunity) -> str:
    return (opportunity.job_title_normalized or opportunity.job_title_raw or "").strip()


def _candidate_title(candidate, roles: list, today: date) -> str:
    """The title the roles say, falling back to the one on the candidate row.

    `derive` is the same function the candidate panel reads, so the title the
    score is computed from is the title the recruiter is looking at.
    """
    derived = derive(roles, today) if roles else None
    title = (derived.current_title if derived else None) or getattr(
        candidate, "current_title", None
    )
    return (title or "").strip()


def _title_component(opportunity, candidate, roles: list, today: date, weight: Decimal):
    job = _job_title(opportunity)
    if not job:
        return _absent(TITLE, weight, "The job order records no job title.")
    theirs = _candidate_title(candidate, roles, today)
    if not theirs:
        return _absent(TITLE, weight, "This candidate has no title on record.")
    # Containment on the job side: a candidate whose title is *more* specific
    # than the vacancy still holds every word the vacancy asked for.
    return _scored(TITLE, weight, _decimal(overlap(job, theirs)))


def _skill_set(values) -> set[str]:
    """Normalised skills, punctuation intact.

    Deliberately not `tokens()`: that strips punctuation, which is right for
    titles and wrong here, because it turns `C`, `C++` and `C#` into the same
    word and would match a C programmer onto a C# vacancy.
    """
    normalised = set()
    for value in values or ():
        if not value:
            continue
        cleaned = normalize_skill(value)
        if cleaned:
            normalised.add(cleaned)
    return normalised


def _skills_component(opportunity, skills: list, weight: Decimal):
    wanted = _skill_set(opportunity.skills)
    if not wanted:
        return _absent(SKILLS, weight, "The job order lists no skills.")
    held = _skill_set(
        getattr(s, "skill_normalized", None) or getattr(s, "skill", None) for s in skills or ()
    )
    if not held:
        return _absent(SKILLS, weight, "This candidate has no skills on record.")
    matched = wanted & held
    note = (
        f"Matched {len(matched)} of {len(wanted)}: {', '.join(sorted(matched))}."
        if matched
        else f"None of the {len(wanted)} skills asked for are on record."
    )
    return _scored(SKILLS, weight, _decimal(len(matched)) / _decimal(len(wanted)), note)


def _employer_component(opportunity, roles: list, weight: Decimal):
    hiring = normalize_company_name(opportunity.company_name_normalized or "")
    if not hiring:
        return _absent(EMPLOYER, weight, "The job order names no company.")
    employers = {
        normalize_company_name(
            getattr(role, "employer_normalized", None) or getattr(role, "employer", None) or ""
        )
        for role in roles or ()
    }
    employers.discard("")
    if not employers:
        return _absent(EMPLOYER, weight, "This candidate has no employers on record.")
    if hiring in employers:
        return _scored(EMPLOYER, weight, _ONE, "Has worked for the hiring company before.")
    return _scored(EMPLOYER, weight, _ZERO, "Has not worked for the hiring company.")


def _salary_component(opportunity, candidate, weight: Decimal):
    fit = salary_fit(
        getattr(candidate, "expected_salary", None),
        getattr(candidate, "salary_currency", None),
        getattr(candidate, "salary_period", None),
        opportunity.salary_min,
        opportunity.salary_max,
        opportunity.salary_currency,
        opportunity.salary_period,
    )
    if fit is None:
        # `salary_fit` also abstains on a currency mismatch, because converting
        # at a rate nobody recorded would be inventing the comparison.
        return _absent(
            SALARY,
            weight,
            "No comparable salary: one side is missing, or the currencies differ.",
        )
    return _scored(SALARY, weight, _decimal(fit))


def _spans(roles: list, today: date) -> list[tuple[date, date]]:
    """Month intervals for every role that is dated and not rejected.

    Rejected roles are the ones a human said did not happen, so counting them
    would credit experience the record denies.
    """
    return [
        span_months(
            role.started_on,
            role.started_precision,
            role.ended_on,
            role.ended_precision,
            today,
        )
        for role in roles or ()
        if getattr(role, "started_on", None) is not None
        and getattr(role, "status", None) != "rejected"
    ]


def _tenure_component(spans: list[tuple[date, date]], weight: Decimal):
    if not spans:
        return _absent(TENURE, weight, "No dated roles, so length of experience is unknown.")
    months = union_months(spans)
    horizon = _decimal(settings.SOURCING_TENURE_FULL_MONTHS)
    return _scored(
        TENURE,
        weight,
        _decimal(months) / horizon,
        f"{months} months of experience across the roles on record.",
    )


def _recency_component(spans: list[tuple[date, date]], today: date, weight: Decimal):
    if not spans:
        return _absent(RECENCY, weight, "No dated roles, so time out of work is unknown.")
    # An open role's span already runs to the current month, so somebody still
    # in post has a gap of zero without being special-cased.
    latest = max(end for _, end in spans)
    gap = (today.year - latest.year) * 12 + (today.month - latest.month)
    stale = _decimal(settings.SOURCING_RECENCY_STALE_MONTHS)
    return _scored(
        RECENCY,
        weight,
        _ONE - _decimal(max(gap, 0)) / stale,
        f"Last recorded role ended {max(gap, 0)} months ago.",
    )


def score_candidate(
    opportunity,
    candidate,
    roles: list,
    skills: list,
    *,
    weights: Mapping[str, float | Decimal] | None = None,
    today: date,
) -> tuple[Decimal | None, list[Component]]:
    """Rank one candidate against one job order.

    Returns the total and the components behind it. The total is `None` when
    no component had anything to compare — the caller must then exclude the
    candidate with a note rather than treat the absence as a low score.

    `today` is required rather than defaulted so a run is reproducible: the
    same inputs must give the same answer tomorrow, and a hidden `date.today()`
    would quietly break that for tenure and recency.
    """
    configured = default_weights()
    if weights:
        configured.update(
            {k: v if isinstance(v, Decimal) else _decimal(v) for k, v in weights.items()}
        )

    spans = _spans(roles, today)
    components = [
        _title_component(opportunity, candidate, roles, today, configured[TITLE]),
        _skills_component(opportunity, skills, configured[SKILLS]),
        _employer_component(opportunity, roles, configured[EMPLOYER]),
        _salary_component(opportunity, candidate, configured[SALARY]),
        _tenure_component(spans, configured[TENURE]),
        _recency_component(spans, today, configured[RECENCY]),
    ]

    scored = [c for c in components if c.has_data]
    denominator = sum((c.weight for c in scored), _ZERO)
    if not scored or denominator == _ZERO:
        return None, components

    numerator = sum((c.contribution for c in scored), _ZERO)
    return _round(numerator / denominator), components
