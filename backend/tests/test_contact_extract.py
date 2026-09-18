"""Public-email extraction — the free half of Reveal Contact (§26).

Deterministic, offline: no provider, no page fetch. The assertions that matter
are the honesty boundaries — a role mailbox, a placeholder domain or a filename
artifact must never reach a recruiter; the same address twice must collapse to
one row; and nothing may exceed the cap.

allow-hardcode: the addresses below are test fixtures.
"""

from app.services.contact_extract import MAX_EMAILS, extract_public_emails


def emails(snippets, urls=None):
    return [e["email"] for e in extract_public_emails(snippets, urls or [""] * len(snippets))]


def test_extracts_a_plain_address_with_its_source():
    out = extract_public_emails(
        ["Contact Jane Tan at jane.tan@acme.com.sg for details."],
        ["https://acme.com.sg/team"],
    )
    assert out == [
        {
            "email": "jane.tan@acme.com.sg",
            "source_url": "https://acme.com.sg/team",
            "verified": False,
        }
    ]


def test_lowercases_and_strips_mailto():
    out = emails(["Reach Jane@ACME.com via mailto:Jane@ACME.com"])
    assert out == ["jane@acme.com"]


def test_dedupes_across_snippets_keeping_first_source():
    out = extract_public_emails(
        ["a@acme.com", "again a@acme.com"],
        ["https://first.example.org", "https://second.example.org"],
    )
    assert [e["email"] for e in out] == ["a@acme.com"]
    assert out[0]["source_url"] == "https://first.example.org"


def test_rejects_role_mailboxes():
    assert emails(["noreply@acme.com donotreply@acme.com postmaster@acme.com"]) == []


def test_rejects_placeholder_and_test_domains():
    assert emails(["a@example.com b@test.example c@foo.test z@real.test"]) == []


def test_rejects_filename_artifacts():
    assert emails(["logo@2x.png hero@3x.jpg"]) == []


def test_rejects_over_length_addresses():
    long_local = "a" * 260
    assert emails([f"{long_local}@acme.com"]) == []


def test_caps_at_max_emails():
    many = " ".join(f"p{i}@acme.com" for i in range(20))
    out = emails([many])
    assert len(out) == MAX_EMAILS


def test_keeps_later_emails_when_an_earlier_one_is_noise():
    out = emails(["noreply@acme.com jane@acme.com"])
    assert out == ["jane@acme.com"]


def test_no_url_at_index_yields_empty_source():
    out = extract_public_emails(["jane@acme.com"], [])
    assert out[0]["source_url"] == ""


def test_trailing_punctuation_is_stripped():
    assert emails(["write to jane@acme.com."]) == ["jane@acme.com"]
