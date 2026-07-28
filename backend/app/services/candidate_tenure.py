"""How long somebody has worked, computed from the roles they held.

Kept apart from the API because it is arithmetic, and arithmetic is worth
testing without a database in the way. Every function here is pure.
"""

from dataclasses import dataclass
from datetime import date

# A year-only date says nothing about the month. Counting from January would
# overstate every such role by up to a year and December would understate it;
# July splits the difference, so the error is bounded and unbiased.
_YEAR_ONLY_MONTH = 7


@dataclass(frozen=True)
class DerivedProfile:
    current_title: str | None
    current_employer: str | None
    years_experience: int | None
    is_current: bool


def _resolve(day: date, precision: str | None) -> date:
    """Pin a stored date to the point its precision actually supports."""
    if precision == "year":
        return date(day.year, _YEAR_ONLY_MONTH, 1)
    return date(day.year, day.month, 1)


def span_months(
    started_on: date,
    started_precision: str | None,
    ended_on: date | None,
    ended_precision: str | None,
    today: date,
) -> tuple[date, date]:
    """The half-open interval a role covers, both ends pinned to a month."""
    start = _resolve(started_on, started_precision)
    end = _resolve(ended_on, ended_precision) if ended_on else date(today.year, today.month, 1)
    if end < start:
        end = start
    return (start, end)


def union_months(spans: list[tuple[date, date]]) -> int:
    """Months covered by at least one span.

    The union rather than the sum: somebody who held two jobs through 2020
    gained one year of experience, not two, and a sum would quietly inflate
    every candidate who ever moonlighted.
    """
    if not spans:
        return 0
    months = 0
    cursor: date | None = None
    for start, end in sorted(spans):
        begin = start if cursor is None or start > cursor else cursor
        if end > begin:
            months += (end.year - begin.year) * 12 + (end.month - begin.month)
            cursor = end
        elif cursor is None or end > cursor:
            cursor = end
    return months


def derive(roles: list, today: date) -> DerivedProfile:
    """What the candidate row should say, given these roles."""
    live = [r for r in roles if getattr(r, "status", None) != "rejected"]
    if not live:
        return DerivedProfile(None, None, None, False)

    spans = [
        span_months(r.started_on, r.started_precision, r.ended_on, r.ended_precision, today)
        for r in live
        if r.started_on is not None
    ]
    months = union_months(spans) if spans else None

    # Current means open-ended and latest-started. Failing that, the role that
    # ended most recently — a candidate between jobs still has a last employer,
    # and the panel labels it "Most recently" rather than claiming otherwise.
    open_ended = [r for r in live if r.started_on is not None and r.ended_on is None]
    if open_ended:
        latest = max(open_ended, key=lambda r: r.started_on)
        is_current = True
    else:
        ended = [r for r in live if r.ended_on is not None]
        if ended:
            latest = max(ended, key=lambda r: r.ended_on)
        else:
            latest = live[0]
        is_current = False

    return DerivedProfile(
        current_title=latest.title,
        current_employer=latest.employer,
        years_experience=months // 12 if months is not None else None,
        is_current=is_current,
    )
