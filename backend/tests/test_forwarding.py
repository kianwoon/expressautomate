"""Extracting the original sender from a forwarded email body.

Outlook/Exchange stamps a ``From: NAME <email>`` / ``Sent:`` block into the
body when a message is forwarded. That block names the person who started the
conversation — the one with the client relationship — not the person who
forwarded it into our mailbox. Graph's ``from`` field only ever names the
forwarder.
"""

from app.services.ingest.forwarding import extract_original_sender


def test_outlook_forwarding_header_is_parsed():
    """The standard Outlook forwarding block yields the original sender."""
    source = (
        "SUBJECT: FW: Vacancy\n"
        "SENDER: jocelynchan@recruitexpress.com.sg\n"
        "Regards,\n"
        "Jocelyn Chan\n"
        "From: Topaz Liang | Recruit Express <topaz@recruitexpress.com.sg>\n"
        "Sent: Tuesday, 4 August 2026 4:29 pm\n"
        "To: Someone <someone@example.com>\n"
        "Subject: Looking for Project Manager\n"
        "Dear All,\n"
        "Wearnes Automotive needs a PM.\n"
    )
    result = extract_original_sender(source)
    assert result is not None
    assert result.email == "topaz@recruitexpress.com.sg"
    assert "Topaz Liang" in result.name


def test_no_forwarding_header_returns_none():
    """A direct email has no forwarding block — returns None (use envelope sender)."""
    source = (
        "SUBJECT: Vacancy at Toyota\n"
        "SENDER: hr@toyota.com.sg\n"
        "We need a service advisor.\n"
    )
    assert extract_original_sender(source) is None


def test_from_without_sent_is_not_a_forwarding_header():
    """A 'From:' in body prose (a signature, a quote) is not a forwarding header.

    The forwarding header is only a forwarding header when ``From:`` is
    immediately followed by ``Sent:`` — that is the structural signature
    Outlook/Exchange stamps. A recruiter's footer that says ``From: the HR
    desk`` must not be mistaken for one.
    """
    source = (
        "SUBJECT: FW: Vacancy\n"
        "SENDER: forwarder@agency.com\n"
        "From: the HR desk\n"
        "We need someone.\n"
    )
    assert extract_original_sender(source) is None


def test_first_forward_in_a_chain_is_the_original_sender():
    """A chain forwarded three times has three From:/Sent: blocks.

    The first one (deepest in the chain) is the person who started the
    conversation. ``search`` returns the first match, which is correct.
    """
    source = (
        "SENDER: c@agency.com\n"
        "From: B Recruiter <b@agency.com>\n"
        "Sent: Tuesday, 4 August 2026 5:00 pm\n"
        "To: C\n"
        "From: A Recruiter <a@agency.com>\n"
        "Sent: Tuesday, 4 August 2026 4:00 pm\n"
        "To: B\n"
    )
    result = extract_original_sender(source)
    assert result is not None
    assert result.email == "b@agency.com"
