"""Turning what a recruiter typed into something two rows can be compared on.

Total and pure. A blank cell, a phone number with a typo, and a name in an
angle-bracketed header are all normal input here, and each returns a value
rather than raising.
"""

import re

import phonenumbers

from app.core.config import settings

_WHITESPACE = re.compile(r"\s+")
_ANGLE = re.compile(r"<([^>]+)>")


def normalize_phone(raw: str | None) -> str | None:
    """E.164, or None when the number cannot be parsed confidently.

    None is the honest answer for rubbish. A half-parsed number used as an
    identity key is worse than none at all: it silently splits one person into
    two records, or merges two people into one.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw, settings.DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )


def is_matchable_phone(e164: str | None) -> bool:
    """Whether this number identifies a person rather than a switchboard.

    A fixed line belongs to a company, so matching a candidate on one would
    merge every colleague who ever listed the office number into one record.
    Such a number is still stored and still displayed — it simply never
    decides that two rows are the same person.
    """
    if not e164:
        return False
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return False
    national = str(parsed.national_number)
    return bool(national) and national[0] in settings.MOBILE_PREFIXES


def normalize_email(raw: str | None) -> str | None:
    """Lowercased address, or None if there isn't one.

    Handles the angle-bracket header form, because a pasted address often
    arrives as `Jane Tan <jane@acme.sg>` and storing that whole string as an
    identity key would make the same person fail to match themselves.
    """
    if not raw:
        return None
    text = raw.strip()
    match = _ANGLE.search(text)
    if match:
        text = match.group(1).strip()
    if text.count("@") != 1:
        return None
    local, _, domain = text.partition("@")
    if not local or not domain or "." not in domain:
        return None
    return f"{local}@{domain}".lower()


def normalize_skill(raw: str) -> str:
    """Lowercase, collapse whitespace. Deliberately blunt.

    A cleverer normaliser that stemmed or aliased would make "Java" and
    "JavaScript" collide, which is worse than two rows a recruiter can read.
    """
    return _WHITESPACE.sub(" ", raw.lower()).strip()
