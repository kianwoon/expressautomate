"""Quote verification and quality state (plan Task 6).

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
    monkeypatch.setattr(settings, "SALARY_CURRENCY_SYMBOLS", "S$=SGD,$=SGD,RM=MYR,£=GBP")
    monkeypatch.setattr(settings, "SALARY_MIN_CREDIBLE", 100.0)
    monkeypatch.setattr(settings, "SALARY_MAX_CREDIBLE", 10_000_000.0)


def _field(**kwargs):
    return ExtractedField(**kwargs)


def _unvalidated(**kwargs):
    """Build a field without running the validator.

    Used where the point of the test is what evidence.py does with a claim,
    independent of whether the schema would have let it through — including
    fields with no offsets at all, which are now legal but would make the
    intent of a fixture ambiguous if built the ordinary way.
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


def test_verification_records_where_the_quote_actually_is():
    """The stored offsets are the ones this module found, not the model's.

    `extraction_evidence.start_char` is meant to point a reviewer at the words
    in the email. Left as the model's arithmetic it points a few characters
    off, at the middle of the previous word.
    """
    start = SOURCE.index("up to $3500")
    field = _field(
        value="3500",
        evidence="up to $3500",
        start_char=start + 7,
        end_char=start + 7 + len("up to $3500"),
        confidence=0.9,
    )

    assert verify(field, SOURCE) is True
    assert field.start_char == start
    assert SOURCE[field.start_char : field.end_char] == "up to $3500"


def test_a_fabricated_quote_fails_even_when_it_sounds_right():
    """The whole point: the model can invent the evidence, not the email."""
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


def test_an_absurd_offset_on_an_invented_quote_still_fails_rather_than_raising():
    """Offsets past the end of the source must not reach an index error."""
    field = _unvalidated(
        value="x", evidence="x", start_char=99_000, end_char=99_010, confidence=0.9
    )

    assert verify(field, SOURCE) is False


def test_an_absurd_offset_on_a_real_quote_is_repaired():
    """Offsets are a hint. A hint being nonsense costs the field nothing when
    the quotation itself is verbatim — the quote is what §15 rests on."""
    field = _unvalidated(
        value="3500",
        evidence="up to $3500",
        start_char=99_000,
        end_char=99_011,
        confidence=0.9,
    )

    assert verify(field, SOURCE) is True
    assert SOURCE[field.start_char : field.end_char] == "up to $3500"


def test_a_near_miss_offset_is_repaired_rather_than_rejected():
    """The inversion. Models quote accurately and count characters badly.

    Rejecting a verbatim quotation for offsets three characters out threw away
    whole correct extractions in production — a long, correctly quoted
    `job_description` failed our own validator. The quote is the claim; where
    it sits is arithmetic, and this module does the arithmetic.
    """
    start = SOURCE.index("up to $3500")
    field = _field(
        value="3500",
        evidence="up to $3500",
        start_char=start + 3,
        end_char=start + 3 + len("up to $3500"),
        confidence=0.99,
    )

    assert verify(field, SOURCE) is True
    assert field.start_char == start


def test_a_repeated_quote_resolves_to_the_occurrence_the_model_pointed_at():
    """The one job the model's offsets still do: telling two copies apart."""
    source = "Location: Singapore. Regards, Evelyn. ACME Pte Ltd, Singapore."
    second = source.rindex("Singapore")
    field = _unvalidated(
        value="Singapore",
        evidence="Singapore",
        start_char=second + 2,
        end_char=second + 2 + len("Singapore"),
        confidence=0.9,
    )

    assert verify(field, source) is True
    assert field.start_char == second


def test_a_quote_found_across_collapsed_whitespace_points_at_real_characters():
    """The offsets are into the original source, not into the normalised copy,
    or a reviewer opening the email at `start_char` lands nowhere near it."""
    source = "Role:\n\n   Finance   officer\n\nat KLN."
    field = _unvalidated(
        value="Finance officer", evidence="finance officer", confidence=0.9
    )

    assert verify(field, source) is True
    assert source[field.start_char : field.end_char] == "Finance   officer"


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
    # The bare "$" is SGD in this deployment, so the currency is named too.
    assert parse_salary("$5,000-$7,000") == (5000.0, 7000.0, "SGD")


def test_a_bare_dollar_is_read_as_sgd():
    """This deployment serves Singapore: a bare "$" is the same dollars as
    "S$", and a band the email stated must not abstain for want of a code."""
    assert parse_salary("$5,500 - $6,400") == (5500.0, 6400.0, "SGD")
    assert parse_salary("S$5,500") == (5500.0, 5500.0, "SGD")


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
    """Reproduced: "KLN" and "OTE" were filed as currency codes.

    "OTE 90,000" carries no "$" so the symbol map cannot rescue it — the only
    thing that could name a currency is the 3-letter scan, and "OTE" must not
    pass it.
    """
    assert parse_salary("KLN pays 3500") == (3500.0, 3500.0, None)
    assert parse_salary("OTE 90,000") == (90000.0, 90000.0, None)


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


# --------------------------------------------------------------------------- //
# structured salary bounds — the additive-sum corroboration rule
# --------------------------------------------------------------------------- //

# The compound-offer source: "$4500 basic max + $800 Rotating shift allowance;
# $3500 for fresh Deg; $2700 for fresh dip". The deterministic parser refuses
# it (four figures), which is exactly why the model emits structured bounds.
COMPOUND_SOURCE = (
    "Contract Biotechnologist. Salary up to $4500 basic max + $800 Rotating "
    "shift allowance; $3500 for fresh Deg; $2700 for fresh dip and above "
    "depending on exp."
)


def test_a_salary_max_equal_to_basic_plus_allowance_verifies():
    """The whole point of the sum rule: 5300 = 4500 + 800, both quoted.

    Without the rule this would fail — 5300 is nowhere in the email — and the
    compound offer would have no usable benchmark. With it, the derived figure
    is trusted because the quote shows the email adding the two figures.
    """
    start = COMPOUND_SOURCE.index("$4500 basic max + $800 Rotating shift allowance")
    quote = "$4500 basic max + $800 Rotating shift allowance"
    field = _field(
        value="5300",
        evidence=quote,
        start_char=start,
        end_char=start + len(quote),
        confidence=0.95,
    )

    assert verify(field, COMPOUND_SOURCE, allow_salary_sum=True) is True


def test_a_salary_max_sum_is_refused_without_the_sum_flag():
    """The sum rule is opt-in for the two bound fields only.

    Every other field in the pipeline must still quote its numbers verbatim:
    the rule exists for `salary_min`/`salary_max` precisely because they are
    the fields a compound offer needs, and it must not leak into, say,
    `working_hours` or `requirements`.
    """
    start = COMPOUND_SOURCE.index("$4500 basic max + $800 Rotating shift allowance")
    quote = "$4500 basic max + $800 Rotating shift allowance"
    field = _field(
        value="5300",
        evidence=quote,
        start_char=start,
        end_char=start + len(quote),
        confidence=0.95,
    )

    assert verify(field, COMPOUND_SOURCE) is False


def test_a_salary_min_from_a_quoted_figure_still_verifies_directly():
    """The floor needs no arithmetic: 2700 is stated, so the plain rule holds."""
    start = COMPOUND_SOURCE.index("$2700 for fresh dip")
    quote = "$2700 for fresh dip"
    field = _field(
        value="2700",
        evidence=quote,
        start_char=start,
        end_char=start + len(quote),
        confidence=0.95,
    )

    assert verify(field, COMPOUND_SOURCE, allow_salary_sum=True) is True


def test_a_sum_needs_an_additive_marker():
    """A tiered list is alternatives, not an addition — a claimed 6200 = 3500
    + 2700 must not verify, because the email says "for fresh Deg; for fresh
    dip", which is a choice of levels, not an arithmetic sum."""
    start = COMPOUND_SOURCE.index("$3500 for fresh Deg; $2700 for fresh dip")
    quote = "$3500 for fresh Deg; $2700 for fresh dip"
    field = _field(
        value="6200",
        evidence=quote,
        start_char=start,
        end_char=start + len(quote),
        confidence=0.95,
    )

    assert verify(field, COMPOUND_SOURCE, allow_salary_sum=True) is False


def test_a_sum_must_use_exactly_two_quoted_figures():
    """The rule is deliberately narrow: a single claimed figure may equal the
    sum of exactly two quoted figures. A claimed 8000 = 4500+3500 drawn from a
    quote containing 4500, 800 and 3500 is a three-figure quote, refused —
    there is no way to tell which pair the email meant, so none is trusted."""
    start = COMPOUND_SOURCE.index("$4500 basic max + $800 Rotating shift allowance; $3500")
    quote = "$4500 basic max + $800 Rotating shift allowance; $3500"
    field = _field(
        value="8000",
        evidence=quote,
        start_char=start,
        end_char=start + len(quote),
        confidence=0.95,
    )

    assert verify(field, COMPOUND_SOURCE, allow_salary_sum=True) is False


def test_a_compound_offer_with_verified_bounds_is_not_review():
    """The whole pipeline change in one assertion: the deterministic parser
    refuses the four-figure salary, but the verified structured bounds carry
    the range, so the row is storable — not parked in review."""
    start = COMPOUND_SOURCE.index("$4500 basic max + $800 Rotating shift allowance")
    max_quote = "$4500 basic max + $800 Rotating shift allowance"
    min_start = COMPOUND_SOURCE.index("$2700 for fresh dip")
    min_quote = "$2700 for fresh dip"
    period = COMPOUND_SOURCE.index("Salary up to $4500")
    job = ExtractedJob(
        job_title=_field(
            value="Contract Biotechnologist",
            evidence="Contract Biotechnologist",
            start_char=0,
            end_char=len("Contract Biotechnologist"),
            confidence=0.95,
        ),
        salary=_field(
            value="$4500 basic max + $800 Rotating shift allowance",
            evidence="$4500 basic max + $800 Rotating shift allowance",
            start_char=start,
            end_char=start + len(max_quote),
            confidence=0.95,
        ),
        salary_max=_field(
            value="5300",
            evidence=max_quote,
            start_char=start,
            end_char=start + len(max_quote),
            confidence=0.95,
        ),
        salary_min=_field(
            value="2700",
            evidence=min_quote,
            start_char=min_start,
            end_char=min_start + len(min_quote),
            confidence=0.95,
        ),
        salary_period=_field(
            value="month",
            evidence="Salary up to $4500",
            start_char=period,
            end_char=period + len("Salary up to $4500"),
            confidence=0.95,
        ),
    )

    assert quality_state(job, COMPOUND_SOURCE) == "verified"


def test_a_fabricated_salary_max_still_lands_in_review():
    """The sum rule is not a licence to invent. A claimed 5300 whose quote is
    nowhere in the email fails `locate` exactly like any other fabrication —
    the derived figure is only trusted when the email actually adds two stated
    figures, and the derived figure equals their sum."""
    job = ExtractedJob(
        job_title=_field(
            value="Contract Biotechnologist",
            evidence="Contract Biotechnologist",
            start_char=0,
            end_char=len("Contract Biotechnologist"),
            confidence=0.99,
        ),
        salary_max=_field(
            value="5300",
            evidence="up to $5300",
            start_char=0,
            end_char=10,
            confidence=0.99,
        ),
    )

    assert quality_state(job, COMPOUND_SOURCE) == "needs_review"
