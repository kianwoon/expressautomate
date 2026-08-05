"""`implied_sex` — turning detected shorthand into a sex, or declining to.

Pure tests: the function is pure, and the one subtlety it carries (it reads
`meaning`, not `attribute`, because `C/F` is filed under `race`) is exactly the
kind of thing a pure test pins down where a DB-backed test would only prove the
wiring.
"""

from dataclasses import dataclass

from app.services.sourcing.preference import FEMALE, MALE, implied_sex


@dataclass
class _Code:
    """The shape `implied_sex` reads — `meaning`, and nothing else."""

    code: str
    meaning: str
    attribute: str | None = None


def test_a_single_female_code_implies_female() -> None:
    assert implied_sex([_Code("O/F", "Any race, female", "gender")]) == FEMALE


def test_cf_implies_female_even_though_it_is_filed_under_race() -> None:
    # The trap this module exists to handle: C/F's primary attribute is "race",
    # so filtering on attribute == "gender" would silently miss the most common
    # code. The meaning is where the sex actually lives.
    assert implied_sex([_Code("C/F", "Chinese, female", "race")]) == FEMALE


def test_a_single_male_code_implies_male() -> None:
    assert implied_sex([_Code("O/M", "Any race, male", "gender")]) == MALE


def test_agreeing_codes_implies_that_sex() -> None:
    assert (
        implied_sex(
            [
                _Code("C/F", "Chinese, female", "race"),
                _Code("O/F", "Any race, female", "gender"),
            ]
        )
        == FEMALE
    )


def test_conflicting_codes_implies_nothing() -> None:
    # An email asking for C/F and O/M has stated both sexes; narrowing to
    # either would be guessing which role the client meant.
    assert (
        implied_sex(
            [
                _Code("C/F", "Chinese, female", "race"),
                _Code("O/M", "Any race, male", "gender"),
            ]
        )
        is None
    )


def test_a_code_that_names_no_sex_is_ignored() -> None:
    # Nationality codes, "Singapore Citizen", carry no sex and must not
    # contribute. A run with only non-sex codes narrows nothing.
    assert implied_sex([_Code("SC", "Singapore Citizen", "nationality")]) is None


def test_open_any_gender_implies_nothing() -> None:
    # "any gender" / "any race, any gender" name no sex; O/O must not narrow.
    assert implied_sex([_Code("O/O", "Open — any race, any gender", None)]) is None


def test_a_non_sex_code_alongside_a_sex_code_does_not_block() -> None:
    # SC and C/F together: only one sex is named, so female wins. The non-sex
    # code contributes nothing, which is not the same as conflicting.
    assert (
        implied_sex(
            [
                _Code("SC", "Singapore Citizen", "nationality"),
                _Code("C/F", "Chinese, female", "race"),
            ]
        )
        == FEMALE
    )


def test_no_codes_implies_nothing() -> None:
    assert implied_sex([]) is None


def test_none_implies_nothing() -> None:
    assert implied_sex(None) is None  # type: ignore[arg-type]


def test_case_insensitivity() -> None:
    # A glossary edited to "Chinese, Female" (capital F) still decodes.
    assert implied_sex([_Code("C/F", "Chinese, Female", "race")]) == FEMALE


def test_male_does_not_match_inside_female() -> None:
    # `\bmale\b` must not fire inside "female" — the word-boundary guard. If it
    # did, every female code would register as both sexes and always conflict.
    assert implied_sex([_Code("O/F", "Any race, female", "gender")]) == FEMALE
