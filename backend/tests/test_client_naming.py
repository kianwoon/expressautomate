"""Normalisation decides what the matcher will *propose*, never what it accepts.

Which is why it stays blunt. A cleverer normaliser that folded "Acme
Engineering" into "Acme" would generate confident-looking proposals across
unrelated companies, and a recruiter clicking through a review queue has no
way to tell a good proposal from a plausible one.
"""

import pytest

from app.services.client_naming import domain_of, normalize_company_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Pte Ltd", "acme"),
        ("ACME PTE. LTD.", "acme"),
        ("  Acme   Holdings  ", "acme holdings"),
        ("Acme Pte Ltd.", "acme"),
        ("Acme Private Limited", "acme"),
        ("Acme, Inc.", "acme"),
        ("Acme LLC", "acme"),
    ],
)
def test_strips_legal_suffixes_and_collapses_space(raw: str, expected: str) -> None:
    assert normalize_company_name(raw) == expected


def test_a_name_that_is_only_a_suffix_survives() -> None:
    # Stripping to empty would make every such row collide with every other.
    assert normalize_company_name("Ltd") == "ltd"


def test_empty_input_normalizes_to_empty() -> None:
    assert normalize_company_name("   ") == ""


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("jane@Acme.com.SG", "acme.com.sg"),
        ("jane@acme.com", "acme.com"),
        ("jane@gmail.com", None),          # free provider: identifies a person
        ("JANE@GMAIL.COM", None),
        (None, None),                       # sender_email is nullable
        ("not-an-email", None),
        ("", None),
    ],
)
def test_domain_of_rejects_free_providers_and_junk(email: str | None, expected: str | None) -> None:
    assert domain_of(email) == expected
