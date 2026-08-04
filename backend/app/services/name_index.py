"""The A–Z index over a name column — shared by candidates and clients.

Both the candidates page and the clients page expose a "jump to first letter"
bar, and both must compute the same first-letter bucket for a name: accent-
folded, whitespace-trimmed, and collapsed to `#` when it is not a Latin
letter. Two copies of that logic — one per module — was how they drifted in
the first place, so it lives here once and both call it.

Postgres' `unaccent()` would say the same thing in one call, but it is an
extension this database does not have installed and enabling it would need a
migration this concern is not entitled to add.
"""

import re
import string
import unicodedata

from sqlalchemy import case, func

# The bucket a name falls into in the A–Z index bar. `#` is everything that is
# not a Latin letter: digits, punctuation, and every non-Latin script an agency
# in Singapore actually stores.
OTHER_INITIAL = "#"
LETTERS = tuple(string.ascii_uppercase)

_LATIN_LETTER_NAME = re.compile(r"^LATIN CAPITAL LETTER ([A-Z]) WITH ")


def _accent_fold_table() -> tuple[str, str]:
    """The accented Latin letters, paired with the plain ones they fold onto.

    Derived from Unicode decomposition rather than typed out: É is E plus a
    combining acute, so stripping the marks recovers the letter a recruiter
    would actually click.

    Only Latin scripts fold. CJK and Tamil have no A–Z letter to fold onto and
    belong in `#` — that is the bucket's whole purpose, not a gap in this table.

    Two ranges, because one is not enough for this vertical: the first covers
    Latin-1 and Latin Extended A/B, the second Latin Extended Additional, which
    is where Vietnamese lives. A Singapore agency places Vietnamese candidates,
    and without the second range every Nguyễn in the database sits under `#`.

    Both cases are emitted even though the expression uppercases first. Whether
    `upper('é')` yields `'É'` is the database's collation's business, and under
    C collation it does not — folding the lowercase form too costs a few
    characters in a `translate()` argument and removes the dependency.

    Two passes, because decomposition alone is not enough. É is E plus a
    combining acute and decomposes; Đ, Ø and Ł are atomic codepoints that do
    not, so stripping marks leaves them untouched and they would land in `#`.
    That is not an edge case here — Đặng and Đỗ are among the commonest
    Vietnamese surnames. The second pass reads the letter out of the
    character's own Unicode name, which is where that fact is recorded.
    """
    accented, plain = [], []
    for codepoint in [*range(0xC0, 0x250), *range(0x1E00, 0x1F00)]:
        char = chr(codepoint)
        upper = char.upper()
        base = "".join(
            part for part in unicodedata.normalize("NFD", upper)
            if not unicodedata.combining(part)
        )
        if len(base) != 1 or base not in LETTERS:
            # e.g. "LATIN CAPITAL LETTER D WITH STROKE" — the letter is the
            # word before WITH. Anything not shaped like that (Æ, the IPA
            # block) has no single letter to fold onto and belongs in `#`.
            # The length check catches ß, whose uppercase is the two-character
            # "SS" and so names no single codepoint to ask about.
            name = unicodedata.name(upper, "") if len(upper) == 1 else ""
            match = _LATIN_LETTER_NAME.match(name)
            if match is None:
                continue
            base = match.group(1)
        accented.append(char)
        plain.append(base)

    # Ð (U+00D0 ETH) is not Đ (U+0110 D WITH STROKE), but on screen it is the
    # same glyph, and its Unicode name says only "ETH" — no "WITH", so neither
    # pass above reaches it. A Vietnamese name typed on a Latin-1 keyboard
    # lands on this codepoint, and leaving it in `#` would file two identical-
    # looking surnames in two different places. Named explicitly because it is
    # a judgement about our data, not a rule Unicode states.
    for char, base in (("Ð", "D"), ("ð", "D")):
        accented.append(char)
        plain.append(base)
    return "".join(accented), "".join(plain)


_FOLD_FROM, _FOLD_TO = _accent_fold_table()


def initial_of(name_column):
    """A SQL expression yielding the A–Z bucket for a name column.

    Takes the column rather than closing over a specific one: the candidates
    list reads it off `Candidate.full_name` and the availability aggregate off
    a subquery, while the clients list reads it off `Client.name`. Both the
    filter and the aggregate in each module must call the same function: two
    expressions that disagreed by one character would put a letter in the bar
    that returns nothing.

    The first *non-whitespace* character, via Postgres' regex form of
    `substring`. Trimming matters: a name imported as " alice" would otherwise
    index under `#`, and nobody would think to look there.

    Membership rather than `BETWEEN 'A' AND 'Z'`: a range comparison is
    resolved by the database's collation, under which accented letters sort
    inside the range and would be mislabelled as plain Latin ones.
    """
    first = func.upper(func.substring(name_column, "[^[:space:]]"))
    first = func.translate(first, _FOLD_FROM, _FOLD_TO)
    return case((first.in_(LETTERS), first), else_=OTHER_INITIAL)


def sorted_initials(found: list[str]) -> list[str]:
    """Letters ascending, `#` last — the reading order of the bar itself."""
    letters = sorted(value for value in found if value != OTHER_INITIAL)
    return letters + ([OTHER_INITIAL] if OTHER_INITIAL in found else [])
