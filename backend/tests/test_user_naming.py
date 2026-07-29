"""Which name a reader is given, and what counts as not having one.

The two callers face different people — a colleague on an internal timeline
and a candidate receiving a WhatsApp message — so they resolve differently,
and only one of them may ever fall back to an email address.
"""

import pytest

from app.services.user_naming import actor_name, recruiter_name


@pytest.mark.parametrize(
    ("preferred", "display", "expected"),
    [
        ("Wong", "Wong Kian Woon", "Wong"),
        (None, "Wong Kian Woon", "Wong Kian Woon"),
        (None, None, None),
        # A claim the provider filled with whitespace is a claim we did not
        # really receive. Left as-is it renders "This is    from ABC.".
        (None, "   ", None),
        (None, "", None),
        # Padding survives storage but must not survive into the sentence.
        (None, "  Wong  ", "Wong"),
        ("  Wong  ", None, "Wong"),
    ],
)
def test_a_candidate_is_never_shown_a_name_we_do_not_have(preferred, display, expected) -> None:
    assert recruiter_name(preferred, display) == expected


def test_a_colleague_may_be_shown_an_email_because_they_work_here() -> None:
    assert actor_name(None, None, "wong@agency.sg") == "wong@agency.sg"
    assert actor_name(None, "Wong Kian Woon", "wong@agency.sg") == "Wong Kian Woon"
    assert actor_name("Wong", "Wong Kian Woon", "wong@agency.sg") == "Wong"


@pytest.mark.parametrize("blank", ["   ", "", "\t"])
def test_a_padded_claim_is_not_a_name_on_the_timeline_either(blank) -> None:
    """The timeline promises to say who acted, so a whitespace-only claim has
    to fall through to the email rather than print a gap where a name goes."""
    assert actor_name(None, blank, "wong@agency.sg") == "wong@agency.sg"
    assert actor_name(blank, None, "wong@agency.sg") == "wong@agency.sg"
    assert actor_name(blank, "  Wong  ", "wong@agency.sg") == "Wong"
