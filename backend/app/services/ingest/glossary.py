"""Deterministic decoding of an agency's shorthand (plan §15).

Recruitment clients write `C/F`, `o/o`, `C/O` and expect a recruiter to read
"Chinese female", "open to all", "Chinese, gender open". A model asked to
expand those would be authoring the meaning, and the meaning is precisely what
`evidence.py` cannot check: "Chinese female" does not appear in an email that
says `C/F`, so every such value would arrive unverifiable and demote its whole
extraction to review.

So the work is split where verifiability splits. The *code* is in the email and
is located here, exactly, with offsets that re-slice to it. The *meaning* comes
from the agency's own glossary row, which the agency wrote and can point at.
The provenance a recruiter sees is therefore "the email said `C/F` at 412–415,
and your glossary defines that as Chinese female" — two checkable halves and no
model in between. It also costs nothing per email, which matters: this runs on
every job order forever.

**What this module deliberately refuses to match**, because a false decode puts
words in a client's mouth:

- Substrings of larger tokens. `C/F` inside `ABC/FGH` is not the code, and the
  naive `in` test that would find it is the single most likely way this feature
  invents a race requirement out of a file path or a part number.
- Anything abutting a separator that would extend the token. `C/F` in `C/F/M`
  is a three-part code, not the two-part one; decoding the prefix would state
  half of what was written and drop the rest.
- Codes shorter than `GLOSSARY_MIN_CODE_LENGTH`. A one-character glossary entry
  matches somewhere in nearly every email, and the agency almost certainly meant
  it as a note to itself rather than as a scanning rule.
- Overlapping matches. When `C/O` and `C/O/F` both fit at one position the
  longer one wins, so the text is read once and never decoded two ways.
- Anything the tenant has not defined. There is no built-in code list anywhere
  in this file — an empty glossary decodes nothing, which is the correct
  behaviour for an agency that has not told us what its shorthand means.

The one ambiguity this cannot resolve is real and worth naming: `c/o` is also
"care of" in a postal address, and nothing in the characters distinguishes the
two. It is left matched rather than guessed at, because the guess would need a
rule about surrounding words that no agency agreed to — and the recorded
offsets plus the snapshotted meaning mean a wrong decode is visible and
correctable, where a silent skip of a real code is not.
"""

import re
from dataclasses import dataclass

from app.core.config import settings

# The one definition of "the same code" in the tree. Imported, never
# reimplemented: two notions of sameness between the API and the scanner is
# exactly how a code the user can see in their glossary stops matching the
# email that contains it, with nothing on screen to explain why.
from app.models.glossary import normalise


@dataclass(frozen=True)
class GlossaryEntry:
    """One row of `glossary_codes`, as this module needs it."""

    code: str
    meaning: str
    attribute: str | None = None


@dataclass(frozen=True)
class DetectedCode:
    """One occurrence. `code` is the literal text, sliced from the source."""

    code: str
    meaning: str
    attribute: str | None
    start_char: int
    end_char: int


def _separator(part: str) -> str:
    """A run of punctuation, made tolerant of the whitespace around it.

    A separator that is *only* whitespace stays required (`\\s+`): a code
    written `open open` must not match `openopen`. A punctuation separator
    permits spaces on either side and between its characters, because mail
    clients and human typists both insert them without changing the meaning.
    """
    chars = [c for c in part if not c.isspace()]
    if not chars:
        return r"\s+"
    return r"\s*" + r"\s*".join(re.escape(c) for c in chars) + r"\s*"


def _pattern(code: str) -> re.Pattern[str] | None:
    """Turn a glossary code into a token-boundary-aware pattern.

    Built from the **literal** code the agency typed, not from `normalise()`.
    That is deliberate and easy to get wrong: `normalise` answers "are these the
    same code", and to do so it folds punctuation away — so `C/F` and `CF`
    normalise alike. A pattern compiled from the folded form would look for the
    letters alone and match the word "cf" anywhere in prose. `normalise` is used
    for identity below; matching is done against what was written.

    Punctuation and spacing are the only things relaxed: a client who types
    `C / F` on one line and `C/F` on the next wrote the same code both times,
    and failing the spaced one would make the feature look broken at random.
    The alphanumeric runs must appear verbatim (case aside), so `CF` never
    matches `C/F` — dropping the separator would let any two-letter word in the
    email stand in for a demographic code.
    """
    literal = (code or "").strip()
    # Measured on the normalised form, not the literal. `C/` is two characters
    # and one letter, and it is the letter that does the matching — counting
    # the slash would let a punctuation-padded single character through the
    # very check that exists to keep one-character codes from decorating every
    # email with a demographic claim the client never made.
    if len(normalise(literal)) < settings.GLOSSARY_MIN_CODE_LENGTH:
        return None
    parts = [p for p in re.split(r"([^0-9A-Za-z]+)", literal) if p]
    if not any(p[0].isalnum() for p in parts):
        # A code made purely of punctuation has no token to anchor on and would
        # match in the middle of ordinary prose.
        return None
    body = "".join(
        re.escape(part)
        if part[0].isalnum()
        else _separator(part)
        for part in parts
    )
    # `\w` here is the cheap half of the boundary test; `_abuts` below is the
    # half that also refuses an adjacent separator.
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _abuts(source: str, start: int, end: int) -> bool:
    """Is the candidate glued to something that makes it part of a bigger token?

    Only whitespace, the ends of the text, and the punctuation an author uses to
    *separate* things may sit against a code. A `/` or `-` on either side means
    the real token is longer than what matched, and reporting the fragment would
    be reporting a code the email did not contain.
    """
    allowed = settings.GLOSSARY_BOUNDARY_CHARS
    weak = settings.GLOSSARY_WEAK_BOUNDARY_CHARS
    # (the character sitting against the code, the one on its far side)
    neighbours = (
        (source[start - 1] if start else "", source[start - 2] if start > 1 else ""),
        (source[end : end + 1], source[end + 1 : end + 2]),
    )
    for char, beyond in neighbours:
        if not char or char.isspace() or char in allowed:
            continue
        # A weak boundary with a word glued to its far side is not punctuation
        # closing a sentence, it is a separator inside a longer code.
        if char in weak and not beyond.isalnum():
            continue
        return True
    return False


def detect(source: str, entries: list[GlossaryEntry]) -> list[DetectedCode]:
    """Every occurrence of every glossary code in the preprocessed text.

    Longest code first, then leftmost, then non-overlapping: a stretch of text
    is decoded once. Without that ordering `C/O` inside `C/O/F` would win by
    position and the more specific definition the agency wrote would never fire.

    The offsets are asserted to re-slice to the matched text before the match is
    returned, the same discipline `evidence.py` applies to a quotation. An
    offset that does not point at what it claims to is worse than no offset: a
    reviewer follows it, sees unrelated words, and stops trusting the ones that
    were right.
    """
    if not source or not entries:
        return []

    candidates: list[tuple[int, int, GlossaryEntry]] = []
    seen_codes: set[str] = set()
    for entry in entries[: settings.GLOSSARY_MAX_CODES]:
        canonical = normalise(entry.code)
        # A tenant with `C/F` and `c / f` defined twice must not decode the same
        # characters twice; the API's uniqueness rule is the same `normalise`.
        if canonical in seen_codes:
            continue
        seen_codes.add(canonical)
        pattern = _pattern(entry.code)
        if pattern is None:
            continue
        for match in pattern.finditer(source):
            # A code that begins or ends in punctuation leaves the tolerant
            # `\s*` at the edge of the match, which would report a span with
            # whitespace hanging off it. Trim before anything records offsets:
            # the span has to be the code and nothing else.
            start, end = match.start(), match.end()
            while start < end and source[start].isspace():
                start += 1
            while end > start and source[end - 1].isspace():
                end -= 1
            if start == end or _abuts(source, start, end):
                continue
            candidates.append((start, end, entry))

    detected: list[DetectedCode] = []
    taken: list[tuple[int, int]] = []
    for start, end, entry in sorted(
        candidates, key=lambda c: (-(c[1] - c[0]), c[0], c[2].code)
    ):
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            continue
        literal = source[start:end]
        # Not a belt-and-braces check — this is the contract. Everything
        # downstream shows the recruiter `source[start_char:end_char]`.
        assert literal, "matched an empty span"
        taken.append((start, end))
        detected.append(
            DetectedCode(
                code=literal,
                meaning=entry.meaning,
                attribute=entry.attribute,
                start_char=start,
                end_char=end,
            )
        )
    return sorted(detected, key=lambda d: d.start_char)
