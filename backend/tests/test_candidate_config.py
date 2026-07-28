"""Which numbers may identify a person is an operator's judgement, not a literal.

A Singapore office line starts `6` and is shared by everyone at the company, so
matching a candidate on one merges strangers. Which prefixes count as personal
differs by country and changes without a deploy, so it has to be configuration
before the matcher exists to read it.
"""

from app.core.config import settings


def test_candidates_page_limit_is_a_positive_int() -> None:
    assert isinstance(settings.CANDIDATES_PAGE_LIMIT, int)
    assert settings.CANDIDATES_PAGE_LIMIT > 0


def test_default_phone_region_is_a_two_letter_code() -> None:
    assert isinstance(settings.DEFAULT_PHONE_REGION, str)
    assert len(settings.DEFAULT_PHONE_REGION) == 2
    assert settings.DEFAULT_PHONE_REGION.isupper()


def test_mobile_prefixes_are_digits_in_a_frozenset() -> None:
    assert isinstance(settings.MOBILE_PREFIXES, frozenset)
    assert settings.MOBILE_PREFIXES
    assert all(p.isdigit() for p in settings.MOBILE_PREFIXES)


def test_singapore_mobile_prefixes_are_the_default() -> None:
    # 8 and 9 are mobile; 6 is a fixed line and must not be in the set.
    assert {"8", "9"} <= settings.MOBILE_PREFIXES
    assert "6" not in settings.MOBILE_PREFIXES
