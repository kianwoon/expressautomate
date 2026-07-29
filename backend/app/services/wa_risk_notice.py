"""The ban-risk notice a recruiter must acknowledge before first pairing.

Spec 2026-07-29-baileys-gateway-plan.md §9: pairing links the recruiter's own
personal WhatsApp through an *unofficial* client (Baileys), and Meta bans
numbers for that — the number at risk is theirs, not the agency's. The
onboarding screen must restate this plainly and record an acknowledgement
before the first pairing.

**Why the version is derived from the text instead of hand-bumped.** A stored
boolean ("consented") cannot say *what they were told* — only that some
acknowledgement happened. The fix is to serve the text from here, record the
version that was acknowledged, and make the version *be* a hash of the text,
so "what was this recruiter shown?" always has an exact, reproducible answer.
A hand-maintained version string (a date someone forgets to bump) can drift
from the text it names; a content hash structurally cannot — editing
`NOTICE_TEXT` below changes `NOTICE_VERSION` whether or not anyone remembers
to. `test_wa_consent.py` pins this relationship directly rather than trusting
the convention.
"""

import hashlib

# allow-hardcode: user-facing copy, the recruiter-facing ban-risk notice
# (plan §9), not matching logic — same category as the WhatsApp draft
# template in candidate_whatsapp.py. Human-reviewed craft guidance shown
# verbatim to the recruiter, not a scoring/matching oracle.
NOTICE_TEXT = (
    "Connecting WhatsApp links your own personal WhatsApp account to this "
    "app. This uses an unofficial connection to WhatsApp (not WhatsApp's "
    "official Business API), which WhatsApp's owner, Meta, prohibits and can "
    "detect. Meta may ban numbers that use it, permanently and without "
    "warning. The number at risk is your own personal WhatsApp number, not "
    "a number owned by this app or your agency. This app's daily send caps "
    "and spacing between messages reduce that risk but do not remove it — "
    "a ban is still possible even if you stay within them. Only proceed if "
    "you accept that risk for your own number."
)


def _content_version(text: str) -> str:
    """Version = a date prefix (for humans) + a content hash (for honesty).

    The date says roughly when this wording was introduced; the hash is what
    actually ties a recorded version to the exact text a recruiter saw. Any
    edit to `text` changes the hash, so the version cannot silently drift
    from the words behind it.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"2026-07-29-{digest}"


NOTICE_VERSION = _content_version(NOTICE_TEXT)
