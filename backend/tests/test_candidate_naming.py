"""What may identify a person, and what may not.

Every function is total: the pipeline genuinely produces blanks and rubbish,
and a normaliser that raised would fail an entire import over one malformed
cell. `None` means "no usable key", which sends the caller to a different
strategy rather than inventing one.
"""

import pytest

from app.services.candidate_naming import (
    is_matchable_phone,
    normalize_email,
    normalize_phone,
    normalize_skill,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+65 9123 4567", "+6591234567"),
        ("9123 4567", "+6591234567"),      # bare local number, default region
        ("9123-4567", "+6591234567"),
        ("6591234567", "+6591234567"),
        ("+65 6123 4567", "+6561234567"),  # office line: parses fine
        ("", None),
        (None, None),
        ("not a phone", None),
        ("12", None),                       # too short to be anyone's number
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("e164", "matchable"),
    [
        ("+6591234567", True),    # mobile
        ("+6581234567", True),    # mobile
        ("+6561234567", False),   # fixed line — shared by a whole company
        (None, False),
    ],
)
def test_only_personal_numbers_may_identify_someone(e164: str | None, matchable: bool) -> None:
    assert is_matchable_phone(e164) is matchable


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jane@Acme.SG", "jane@acme.sg"),
        ("  jane@acme.sg  ", "jane@acme.sg"),
        ("Jane Tan <jane@acme.sg>", "jane@acme.sg"),
        ("not-an-email", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_email(raw: str | None, expected: str | None) -> None:
    assert normalize_email(raw) == expected


def test_normalize_skill_folds_case_and_space() -> None:
    assert normalize_skill("  Python  3 ") == "python 3"
    assert normalize_skill("PYTHON") == "python"
