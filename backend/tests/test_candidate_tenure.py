"""The arithmetic behind years_experience.

Separated from the API because this is the part most likely to be wrong and
the part cheapest to test: no database, no request, no tenant.
"""

from datetime import date

from app.models.candidate import CandidateRole
from app.services.candidate_tenure import derive, union_months


class _Role:
    """Stands in for a CandidateRole without touching the database."""

    def __init__(
        self,
        employer,
        title,
        started_on,
        ended_on=None,
        started_precision="month",
        ended_precision="month",
    ):
        self.employer = employer
        self.title = title
        self.started_on = started_on
        self.ended_on = ended_on
        self.started_precision = started_precision
        self.ended_precision = ended_precision
        self.status = CandidateRole.CONFIRMED


def test_two_concurrent_roles_count_once():
    """The union, not the sum. Two jobs through 2020 is one year, not two."""
    spans = [(date(2020, 1, 1), date(2021, 1, 1)), (date(2020, 1, 1), date(2021, 1, 1))]
    assert union_months(spans) == 12


def test_partly_overlapping_roles_count_the_covered_months():
    spans = [(date(2020, 1, 1), date(2020, 7, 1)), (date(2020, 4, 1), date(2020, 10, 1))]
    assert union_months(spans) == 9


def test_a_role_wholly_inside_another_adds_nothing():
    """A six-month contract taken during a three-year job is not extra time."""
    spans = [(date(2020, 1, 1), date(2023, 1, 1)), (date(2021, 1, 1), date(2021, 7, 1))]
    assert union_months(spans) == 36


def test_a_gap_between_roles_is_not_counted():
    spans = [(date(2018, 1, 1), date(2019, 1, 1)), (date(2021, 1, 1), date(2022, 1, 1))]
    assert union_months(spans) == 24


def test_an_open_ended_role_counts_up_to_today():
    roles = [_Role("Parkway Shenton", "Staff Nurse", date(2023, 1, 1), None)]
    assert derive(roles, today=date(2026, 1, 1)).years_experience == 3


def test_year_precision_counts_from_mid_year():
    """"2019 to 2021" is somewhere between one and three years.

    July avoids a bias in either direction; January would systematically
    overstate and December understate.
    """
    roles = [
        _Role("Coda", "Engineer", date(2019, 1, 1), date(2021, 1, 1), "year", "year")
    ]
    assert derive(roles, today=date(2026, 1, 1)).years_experience == 2


def test_the_open_ended_role_is_the_current_one():
    roles = [
        _Role("Old Place", "Junior", date(2015, 1, 1), date(2019, 1, 1)),
        _Role("Parkway Shenton", "Staff Nurse", date(2019, 2, 1), None),
    ]
    profile = derive(roles, today=date(2026, 1, 1))
    assert profile.current_employer == "Parkway Shenton"
    assert profile.is_current is True


def test_between_jobs_names_the_most_recent_role_and_says_it_is_not_current():
    roles = [_Role("Old Place", "Junior", date(2015, 1, 1), date(2019, 1, 1))]
    profile = derive(roles, today=date(2026, 1, 1))
    assert profile.current_employer == "Old Place"
    assert profile.is_current is False


def test_a_role_with_no_dates_still_names_the_employer():
    roles = [_Role("Coda", "Engineer", None, None)]
    profile = derive(roles, today=date(2026, 1, 1))
    assert profile.current_employer == "Coda"
    assert profile.years_experience is None


def test_no_roles_derives_nothing():
    profile = derive([], today=date(2026, 1, 1))
    assert profile.current_employer is None
    assert profile.years_experience is None
