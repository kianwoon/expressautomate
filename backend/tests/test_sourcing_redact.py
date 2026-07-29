"""Redaction of protected-attribute codes before any text reaches the model."""

from dataclasses import dataclass

from app.services.sourcing.redact import REDACTION_MARKER, redact


@dataclass
class FakeCode:
    """Stands in for `OpportunityCode` — `redact` only reads two attributes."""

    code: str
    attribute: str | None = None


def test_protected_code_is_removed_and_reported():
    text, removed = redact(
        "Looking for C/F with 3 years experience",
        [FakeCode(code="C/F", attribute="race")],
    )

    assert "C/F" not in text
    assert REDACTION_MARKER in text
    assert removed == ["C/F"]
    # The rest of the requirement survives — only the code goes.
    assert "3 years experience" in text


def test_code_without_attribute_is_left_alone():
    original = "Must accept NS rotation"
    text, removed = redact(original, [FakeCode(code="NS", attribute=None)])

    assert text == original
    assert removed == []


def test_every_occurrence_is_removed():
    text, removed = redact(
        "C/F preferred, ideally C/F with retail background",
        [FakeCode(code="C/F", attribute="race")],
    )

    assert "C/F" not in text
    assert text.count(REDACTION_MARKER) == 2
    # Reported once: the caller is told which code was suppressed, not how often.
    assert removed == ["C/F"]


def test_matching_is_case_insensitive():
    text, removed = redact(
        "wants c/f and C/f candidates",
        [FakeCode(code="C/F", attribute="race")],
    )

    assert "c/f" not in text.lower()
    assert removed == ["C/F"]


def test_text_without_codes_is_unchanged():
    original = "Warehouse assistant, immediate start"
    text, removed = redact(original, [FakeCode(code="C/F", attribute="race")])

    assert text == original
    assert removed == []


def test_empty_code_list_changes_nothing():
    original = "C/F preferred"
    text, removed = redact(original, [])

    assert text == original
    assert removed == []


def test_empty_text_is_safe():
    text, removed = redact("", [FakeCode(code="C/F", attribute="race")])

    assert text == ""
    assert removed == []


def test_blank_code_string_is_ignored():
    original = "Warehouse assistant"
    text, removed = redact(original, [FakeCode(code="   ", attribute="race")])

    assert text == original
    assert removed == []


def test_substring_inside_a_word_is_still_redacted():
    """Over-redaction is the safe failure; under-redaction is not.

    A short code such as `M` sits inside countless ordinary words. Requiring a
    word boundary would let `C/F` glued to punctuation or a longer token slip
    through, and a protected characteristic reaching the model is a worse
    outcome than a mangled sentence. So the match is a plain substring and this
    test pins that behaviour deliberately.
    """
    text, removed = redact(
        "Malay speaker required", [FakeCode(code="M", attribute="race")]
    )

    assert "Malay" not in text
    assert removed == ["M"]


def test_multiple_codes_only_protected_ones_go():
    text, removed = redact(
        "C/F for the NS shift",
        [FakeCode(code="C/F", attribute="race"), FakeCode(code="NS", attribute=None)],
    )

    assert "C/F" not in text
    assert "NS" in text
    assert removed == ["C/F"]


def test_marker_keeps_words_apart():
    """Splicing to an empty string would fuse the words around the code."""
    text, _ = redact("hire C/F urgently", [FakeCode(code="C/F", attribute="race")])

    assert "hire" in text and "urgently" in text
    assert "hireurgently" not in text.replace(" ", "")


def test_regex_metacharacters_in_a_code_are_literal():
    text, removed = redact(
        "wants M(F) staff", [FakeCode(code="M(F)", attribute="gender")]
    )

    assert "M(F)" not in text
    assert removed == ["M(F)"]


def test_duplicate_codes_are_reported_once():
    text, removed = redact(
        "C/F and c/f",
        [FakeCode(code="C/F", attribute="race"), FakeCode(code="c/f", attribute="race")],
    )

    assert "c/f" not in text.lower()
    assert removed == ["C/F"]
