"""Turning what an email says into something two rows can be compared on.

Both functions are total and pure: they return a value or None for every
input, including the nulls the pipeline genuinely produces. `sender_email` is
nullable on `email_messages`, and a matcher that raised on a null sender would
fail an ingest run over a message that is merely unusual.
"""

import re

from app.core.config import settings

# Order matters: the longer forms are tried first, so "Pte Ltd" is not left as
# a dangling "Pte" by an earlier match on "Ltd".
_LEGAL_SUFFIXES = (
    "private limited",
    "pte ltd",
    "pte",
    "sdn bhd",
    "limited",
    "ltd",
    "llc",
    "llp",
    "inc",
    "corp",
    "corporation",
    "co",
    "gmbh",
    "bv",
    "nv",
    "sa",
    "ag",
)

_PUNCTUATION = re.compile(r"[.,]")
_WHITESPACE = re.compile(r"\s+")


def normalize_company_name(raw: str) -> str:
    """Lowercase, drop punctuation and trailing legal suffixes, collapse space.

    Deliberately conservative. This value only ever *proposes* a match to a
    human, so a false negative costs one extra click and a false positive
    costs a recruiter's trust in the review queue.
    """
    text = _PUNCTUATION.sub(" ", raw.lower())
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""

    # Peel suffixes from the end until none matches, so "Acme Pte Ltd" and
    # "Acme Ltd" agree. A name that is *only* a suffix keeps it — normalising
    # "Ltd" to "" would make it collide with every other empty result.
    changed = True
    while changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
                break
    return text


def domain_of(email: str | None) -> str | None:
    """The mail domain, unless it identifies a person rather than a company.

    Returns None for a null or malformed address and for every provider in
    `settings.FREE_EMAIL_DOMAINS`. None means "no domain key available", which
    sends the matcher to name matching rather than inventing one (§15).
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain or domain in settings.FREE_EMAIL_DOMAINS:
        return None
    return domain
