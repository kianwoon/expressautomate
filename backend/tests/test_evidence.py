"""Offset verification and quality state (plan Task 6).

The tests that matter here are the ones where the model is confident and wrong.
Everything else in the pipeline takes the model at its word; this module is the
only place that checks, so a hole here is a fabricated vacancy shown to a
recruiter as fact (§15).

allow-hardcode: the source strings and offsets below are test fixtures.
"""

import pytest

from app.core.config import settings
from app.services.ingest.evidence import parse_salary, quality_state, verify
from app.services.ingest.schema import NOT_MENTIONED, ExtractedField, ExtractedJob

SOURCE = "Finance officer at KLN Logistics. Salary up to $3500 per month."


@pytest.fixture(autouse=True)
def _confidence_floor(monkeypatch):
    """Pin the threshold instead of inheriting whatever the environment has.

    CI has no `.env`, so a test that reads the ambient value is asserting
    against the deployment rather than against the code.
    """
    monkeypatch.setattr(settings, "EXTRACTION_VERIFIED_CONFIDENCE", 0.8)
    monkeypatch.setattr(settings, "SALARY_CURRENCY_CODES", "SGD,USD,MYR,EUR,GBP")
    monkeypatch.setattr(settings, "SALARY_CURRENCY_SYMBOLS", "S$=SGD,RM=MYR,£=GBP")
    monkeypatch.setattr(settings, "SALARY_MIN_CREDIBLE", 100.0)
    monkeypatch.setattr(settings, "SALARY_MAX_CREDIBLE", 10_000_000.0)


def _field(**kwargs):
    return ExtractedField(**kwargs)


def _unvalidated(**kwargs):
    """Build a field the schema would reject.

    `ExtractedField` already refuses a quote whose length disagrees with its
    span, so the only way to exercise what evidence.py does about such a claim
    is to skip validation. Without this the checks below would pass by never
    running.
    """
    kwargs.setdefault("evidence", None)
    kwargs.setdefault("confidence", 0.0)
    return ExtractedField.model_construct(**kwargs)


def test_a_real_span_verifies():
    start = SOURCE.index("up to $3500")
    field = _field(
        value="3500",
        evidence="up to $3500",
        start_char=start,
        end_char=start + len("up to $3500"),
        confidence=0.9,
    )

    assert verify(field, SOURCE) is True


def test_a_fabricated_quote_fails_even_when_it_sounds_right():
    """The whole point: the model can invent the evidence, not the offsets."""
    field = _field(
        value="6000",
        evidence="salary is SGD 6,000",
        start_char=0,
        end_char=19,
        confidence=0.98,
    )

    assert verify(field, SOURCE) is False


def test_whitespace_differences_are_tolerated():
    """Wrapped mail and HTML-to-text collapse runs of spaces differently.

    A quote that differs from the source only in how much whitespace sits
    between the same words is a formatting artefact, not an invention, and
    failing it would send correct extractions to review.
    """
    start = SOURCE.index("up to $3500")
    field = _unvalidated(
        value="3500",
        evidence="up  to   $3500",
        start_char=start,
        end_char=start + len("up to $3500"),
        confidence=0.9,
    )

    assert verify(field, SOURCE) is True


def test_offsets_past_the_end_of_the_source_fail_rather_than_raise():
    """Python slices out of range silently, so an absurd offset would otherwise
    compare an empty string and be indistinguishable from an empty quote."""
    field = _unvalidated(
        value="x", evidence="x", start_char=99_000, end_char=99_010, confidence=0.9
    )

    assert verify(field, SOURCE) is False


def test_a_near_miss_offset_is_a_failure_rather_than_a_fuzzy_match():
    """Models miscount characters, and the temptation is to search nearby.

    Refused: a span that lands one word over still proves nothing about where
    the value came from, and accepting near-misses turns the one deterministic
    check in the pipeline into a heuristic.
    """
    start = SOURCE.index("up to $3500")
    field = _field(
        value="3500",
        evidence="up to $3500",
        start_char=start + 3,
        end_char=start + 3 + len("up to $3500"),
        confidence=0.99,
    )

    assert verify(field, SOURCE) is False


def test_a_missing_field_is_not_a_verification_failure():
    field = _field(value=NOT_MENTIONED, confidence=0.0)

    assert verify(field, SOURCE) is True


def test_high_confidence_with_bad_evidence_is_not_verified():
    """Model confidence is not a calibrated probability and cannot outvote a
    failed deterministic check."""
    job = ExtractedJob(
        job_title=_field(
            value="Finance officer",
            evidence="Finance officer",
            start_char=0,
            end_char=15,
            confidence=0.99,
        ),
        salary=_field(
            value="9999",
            evidence="salary is $9,999",
            start_char=0,
            end_char=16,
            confidence=0.99,
        ),
    )

    assert quality_state(job, SOURCE) == "needs_review"


def test_everything_checking_out_is_verified():
    start = SOURCE.index("up to $3500")
    period = SOURCE.index("per month")
    job = ExtractedJob(
        salary_period=_field(
            value="monthly",
            evidence="per month",
            start_char=period,
            end_char=period + len("per month"),
            confidence=0.95,
        ),
        job_title=_field(
            value="Finance officer",
            evidence="Finance officer",
            start_char=0,
            end_char=15,
            confidence=0.95,
        ),
        salary=_field(
            value="3500",
            evidence="up to $3500",
            start_char=start,
            end_char=start + len("up to $3500"),
            confidence=0.95,
        ),
    )

    assert quality_state(job, SOURCE) == "verified"


def test_a_job_with_nothing_in_it_needs_review():
    """An all-missing extraction is a failed one, not a confident empty answer."""
    job = ExtractedJob(job_title=_field(value=NOT_MENTIONED))

    assert quality_state(job, SOURCE) == "needs_review"


def test_a_salary_with_no_period_is_only_likely():
    """$3500 is a good wage per month and an insulting one per year.

    Without the period the number is not comparable to any other number, so
    every salary analytic built on it is wrong.
    """
    start = SOURCE.index("up to $3500")
    job = ExtractedJob(
        salary=_field(
            value="3500",
            evidence="up to $3500",
            start_char=start,
            end_char=start + len("up to $3500"),
            confidence=0.95,
        ),
    )

    assert quality_state(job, SOURCE) == "likely"


def test_a_salary_carrying_no_figure_needs_review():
    """Verified evidence for a non-number is still an unusable salary."""
    start = SOURCE.index("Salary")
    job = ExtractedJob(
        salary=_field(
            value="competitive",
            evidence="Salary",
            start_char=start,
            end_char=start + len("Salary"),
            confidence=0.95,
        ),
    )

    assert quality_state(job, SOURCE) == "needs_review"


def test_low_confidence_on_verified_evidence_is_likely_not_verified():
    """Deterministic checks can only demote. A real span the model itself
    doubts is worth showing, but not worth showing unflagged."""
    job = ExtractedJob(
        job_title=_field(
            value="Finance officer",
            evidence="Finance officer",
            start_char=0,
            end_char=15,
            confidence=0.4,
        ),
    )

    assert quality_state(job, SOURCE) == "likely"


def test_the_confidence_floor_comes_from_settings(monkeypatch):
    """Raising the bar must change the verdict, or the threshold is decorative."""
    job = ExtractedJob(
        job_title=_field(
            value="Finance officer",
            evidence="Finance officer",
            start_char=0,
            end_char=15,
            confidence=0.85,
        ),
    )
    assert quality_state(job, SOURCE) == "verified"

    monkeypatch.setattr(settings, "EXTRACTION_VERIFIED_CONFIDENCE", 0.95)

    assert quality_state(job, SOURCE) == "likely"


def test_salary_parsing_extracts_a_range():
    assert parse_salary("$5,000-$7,000") == (5000.0, 7000.0, None)


def test_salary_parsing_extracts_a_currency_when_stated():
    assert parse_salary("SGD 6,000") == (6000.0, 6000.0, "SGD")


def test_unparseable_salary_returns_nothing_rather_than_a_guess():
    assert parse_salary("competitive") == (None, None, None)


def test_a_value_contradicting_its_own_quote_does_not_verify():
    """The defect this module exists to prevent, reached through the value.

    Real quote, correct offsets, and a number the email never contained: every
    check passed and $9,000 was filed as a verified salary for a mail that said
    $3,500. The quote is checked against the source, so the quote wins.
    """
    start = SOURCE.index("up to $3500")
    field = _field(
        value="9000",
        evidence="up to $3500",
        start_char=start,
        end_char=start + len("up to $3500"),
        confidence=0.99,
    )

    assert verify(field, SOURCE) is False


def test_a_salary_contradicting_its_quote_lands_in_review_not_verified():
    period = SOURCE.index("per month")
    start = SOURCE.index("up to $3500")
    job = ExtractedJob(
        salary_period=_field(
            value="monthly",
            evidence="per month",
            start_char=period,
            end_char=period + len("per month"),
            confidence=0.99,
        ),
        salary=_field(
            value="9000",
            evidence="up to $3500",
            start_char=start,
            end_char=start + len("up to $3500"),
            confidence=0.99,
        ),
    )

    assert quality_state(job, SOURCE) == "needs_review"


def test_a_normalised_text_value_still_verifies():
    """Corroboration must not punish "monthly" for quoting "per month".

    Only figures are policed; demanding a substring for text fields would send
    every correctly normalised field to review and train reviewers to ignore
    the flag.
    """
    period = SOURCE.index("per month")
    field = _field(
        value="monthly",
        evidence="per month",
        start_char=period,
        end_char=period + len("per month"),
        confidence=0.95,
    )

    assert verify(field, SOURCE) is True


def test_a_thousands_suffix_is_not_read_as_six_dollars():
    """"6k" parsed as 6.0 — a thousand-fold error, silently."""
    assert parse_salary("6k") == (6000.0, 6000.0, None)


def test_working_hours_are_not_read_as_a_minimum_salary():
    """Reproduced: (40.0, 3500.0) — a 40-hour week became the salary floor."""
    assert parse_salary("3500 for 40 hours") == (None, None, None)


def test_a_reference_number_is_not_read_as_a_maximum_salary():
    """Reproduced: (3500.0, 12345.0) — the job reference became the ceiling."""
    assert parse_salary("3500 (Ref #12345)") == (None, None, None)


def test_a_cap_is_not_presented_as_an_exact_figure():
    """Reproduced: (3500.0, 3500.0) — "up to" turned into "pays exactly".

    `salary_raw` keeps the sentence, so reporting only the ceiling loses
    nothing a recruiter needs and stops claiming a commitment never made.
    """
    assert parse_salary("up to 3500") == (None, 3500.0, None)


def test_a_floor_is_not_presented_as_an_exact_figure():
    assert parse_salary("from 3500") == (3500.0, None, None)


def test_a_capped_salary_is_still_usable_enough_to_verify():
    """The cap has a figure, so the row is storable — it must not go to review
    merely because the minimum is unknown."""
    start = SOURCE.index("up to $3500")
    period = SOURCE.index("per month")
    job = ExtractedJob(
        salary_period=_field(
            value="monthly",
            evidence="per month",
            start_char=period,
            end_char=period + len("per month"),
            confidence=0.95,
        ),
        salary=_field(
            value="3500",
            evidence="up to $3500",
            start_char=start,
            end_char=start + len("up to $3500"),
            confidence=0.95,
        ),
    )

    assert quality_state(job, SOURCE) == "verified"


def test_a_shouted_word_is_not_mistaken_for_a_currency():
    """Reproduced: "KLN" and "OTE" were filed as currency codes."""
    assert parse_salary("KLN pays 3500") == (3500.0, 3500.0, None)
    assert parse_salary("OTE $90,000") == (90000.0, 90000.0, None)


def test_the_recognised_currencies_come_from_settings(monkeypatch):
    """Widening the list must change the verdict, or it is decorative."""
    assert parse_salary("KRW 3500000") == (3500000.0, 3500000.0, None)

    monkeypatch.setattr(settings, "SALARY_CURRENCY_CODES", "SGD,KRW")

    assert parse_salary("KRW 3500000") == (3500000.0, 3500000.0, "KRW")


def test_a_currency_symbol_maps_to_a_code():
    assert parse_salary("S$3,500") == (3500.0, 3500.0, "SGD")


def test_the_plausibility_floor_comes_from_settings(monkeypatch):
    assert parse_salary("3500 for 40 hours") == (None, None, None)

    monkeypatch.setattr(settings, "SALARY_MIN_CREDIBLE", 10.0)

    assert parse_salary("3500 for 40 hours") == (40.0, 3500.0, None)


def test_more_figures_than_a_range_can_explain_are_refused():
    """Picking the extremes of a set the parser cannot account for is a guess."""
    assert parse_salary("3500 to 4500, 13th month, 2 years exp") == (None, None, None)
