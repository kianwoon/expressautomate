"""The period a salary is quoted in, as read from what a model actually says.

The prompt asks for one of hour, day, month or year. Models answer "Monthly",
"per annum", "p.a." and " month " anyway, and that answer used to reach the
column verbatim — which is what made the salary sort drop those rows: it looks
the period up by the canonical word, found nothing, and sank the salary.

Normalising is therefore not tidiness. It is what lets the column carry a CHECK
constraint, and what lets the sort match exactly.
"""

import pytest

from app.services.ingest.persist import _salary_period
from app.services.ingest.schema import NOT_MENTIONED, ExtractedField


def _field(value: str) -> ExtractedField:
    # `evidence` is required for any present value — the anti-fabrication rule
    # in ExtractedField — and its content is irrelevant to this function.
    return ExtractedField(value=value, evidence=f"paid {value}")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # What the prompt asks for.
        ("hour", "hour"), ("day", "day"), ("month", "month"), ("year", "year"),
        # What the prompt does not ask for but emails say.
        ("week", "week"), ("weekly", "week"),
        # Casing and padding — the pair that caused the original bug.
        ("Month", "month"), (" month ", "month"), ("MONTH", "month"),
        # Regular derivations, none of them listed anywhere in the source.
        ("monthly", "month"), ("hourly", "hour"), ("per month", "month"),
        ("per-hour", "hour"), ("Per Year", "year"), ("yearly", "year"),
        # The irregulars: neither spelling contains the word it means.
        ("daily", "day"), ("annual", "year"), ("annually", "year"),
        ("per annum", "year"), ("annum", "year"),
    ],
)
def test_a_period_a_model_might_answer_is_read(answer: str, expected: str) -> None:
    assert _salary_period(_field(answer)) == expected


@pytest.mark.parametrize("answer", ["fortnight", "per shift", "???", "   ", "-"])
def test_an_unreadable_period_is_nothing_rather_than_a_guess(answer: str) -> None:
    """§15: "we could not read it" is not "it was monthly".

    Nothing is lost by admitting it — the sentence the model read still sits in
    `salary_raw` and on the evidence row, so a human can see what was said.
    """
    assert _salary_period(_field(answer)) is None


def test_an_absent_period_stays_absent() -> None:
    assert _salary_period(None) is None
    assert _salary_period(ExtractedField(value=NOT_MENTIONED)) is None


def test_nothing_outside_the_vocabulary_can_be_returned() -> None:
    """The constraint's side of the bargain.

    `ck_opportunities_salary_period_known` refuses anything but these five and
    NULL, so a normaliser that could return a sixth value would turn an odd
    model answer into a failed ingestion rather than a missing period.
    """
    from app.services.ingest.persist import _IRREGULAR_PERIODS, _SALARY_PERIODS

    answers = [
        *_SALARY_PERIODS,
        *_IRREGULAR_PERIODS,
        "Monthly", "per annum", "fortnight", "", "  ", "per shift",
    ]
    for answer in answers:
        result = _salary_period(_field(answer)) if answer.strip() else None
        assert result is None or result in _SALARY_PERIODS, answer
