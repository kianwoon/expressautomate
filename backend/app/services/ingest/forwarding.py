"""Finding who first sent a forwarded email.

Outlook and Exchange stamp a forwarding header into the body when a message
is forwarded or replied to. The header looks like:

    From: Topaz Liang | Recruit Express <topaz@recruitexpress.com.sg>
    Sent: Tuesday, 4 August 2026 4:29 pm
    To: ...
    Subject: ...

The person on that ``From:`` line is the original sender — the one who has
the client relationship — not the person who forwarded the mail into our
mailbox. Graph's ``from`` field only ever names the forwarder.

This module extracts that original sender deterministically. The forwarding
header is a structured block (``From:`` followed by ``Sent:`` followed by
``To:`` followed by ``Subject:``), so a regex is cheaper and more reliable
than asking an LLM to find it, and it does not spend a model call.

Only the *first* forwarding block is parsed. A chain that was forwarded
three times has three ``From:`` lines; the first one (deepest in the chain)
is the person who started the conversation, which is the person the client
relationship belongs to.
"""

import re
from dataclasses import dataclass

# Outlook/Exchange forwarding header: ``From: NAME <email>`` or
# ``From: NAME | TITLE <email>``. The name is everything before the last
# ``<email>``, and the email is the angle-bracket address. The line must be
# followed (not necessarily immediately) by ``Sent:`` for it to count as a
# forwarding header rather than a stray ``From:`` in the body prose — a
# recruiter's signature that says "From: the HR desk" would otherwise match.
#
# ``[^\n]*`` on the name is load-bearing. A ``From:`` line without an
# angle-bracket address (a reply header such as "From: Jocelyn Chan |
# Recruit Express" whose next line is ``Sent:``) must fail to match rather
# than let the name group run on past newlines hunting for some later
# ``<email>`` — with DOTALL that hunt swallowed the entire email body into
# a buddy's name (production, 2026-09-02: "Jocelyn Chan | Recruit Express
# Sent: Tuesday, 1 September …" — a thousand-character name keyed on the
# *recipient's* address). A name never spans lines; if the address is not
# on the same line, this is not a parseable forwarding header.
_FORWARD_HEADER = re.compile(
    r"From:\s*([^\n]*?)<([^<>@\n]+@[^<>\n]+)>\s*\nSent:",
    re.IGNORECASE,
)

# The name can carry a display title with pipe separators
# ("Topaz Liang | Recruit Express") — keep the full string so the recruiter
# recognises it, but trim whitespace from the edges.
_NAME_TRIM = re.compile(r"\s+")


@dataclass(frozen=True)
class OriginalSender:
    """Who sent the email before it was forwarded into our mailbox.

    ``None`` for every field means "no forwarding header was found — this is a
    direct email" and the caller should fall back to the envelope sender.
    """

    name: str
    email: str


def extract_original_sender(source: str) -> OriginalSender | None:
    """Parse the original sender from the first forwarding header in the body.

    Returns None when there is no forwarding header (a direct email, or a
    forward from a client that did not stamp a standard header). The caller
    then falls back to the envelope sender, which is the right person for a
    direct email.
    """
    match = _FORWARD_HEADER.search(source)
    if match is None:
        return None
    name = _NAME_TRIM.sub(" ", match.group(1)).strip()
    email = match.group(2).strip()
    if not email or "@" not in email:
        return None
    return OriginalSender(name=name or email, email=email)
