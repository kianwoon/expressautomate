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
    # Reject inputs with alphabetic characters. The phonenumbers library
    # strips embedded letters per RFC3966, silently turning "9123a4567" into
    # "+6591234567" — a different valid number. As an identity key, this
    # corrupts matching and must fail confidently instead.
    if re.search(r"[a-zA-Z]", raw):
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


def normalize_language(raw: str) -> str:
    """Exactly `normalize_skill`, and deliberately no more.

    The temptation here is an alias table — folding "Bahasa" onto "Malay", or
    "Filipino" onto "Tagalog". Both of those are a guess about a person, and
    for a domestic-worker placement they are guesses that matter: Bahasa
    Melayu and Bahasa Indonesia are different languages, and a recruiter who
    typed one must not find the other in the record (§15).

    Kept as its own function rather than a call site of `normalize_skill`
    because the two are the same rule for different reasons, and a future
    change to skill normalisation must not silently move languages with it.
    """
    return _WHITESPACE.sub(" ", raw.lower()).strip()
