"""Take protected-attribute codes out of opportunity text before a model reads it.

The glossary exists because recruiters write requirements in shorthand, and some
of that shorthand names a protected characteristic — race, nationality, gender,
age, religion, marital status. If such a code reaches the model that explains why
a candidate fits, the model can rank people on it, and the platform becomes the
thing that launders a discriminatory filter. This module removes those codes from
every piece of text the prompt carries.

**Redaction matches the verbatim `code` string, not the recorded offsets.**
`OpportunityCode.start_char`/`end_char` index the *source email*, because
`detect(source, entries)` in `app/services/ingest/glossary.py` scans the whole
message. The text redacted here is the *extracted* title, description and
requirements, so those offsets point at unrelated positions: slicing by them
would cut out innocent words while leaving the code itself in place. The literal
string is the only thing that survives the move from email to extracted field.

**The limit, which a reader must not mistake.** This catches *coded*
discrimination — the shorthand the glossary was built to decode. It does nothing
about "female preferred" written out in plain words, because there is no code to
match. That is why this is one layer of three: the sourcing prompt also instructs
the model to refuse protected-attribute reasoning, and whatever the model reports
noticing is stored on the run so a human can audit it. Trusting this module alone
would be trusting it further than it deserves.

Pure module — no database, no settings, no I/O.
"""

import re

# A marker rather than an empty string: splicing the code out entirely would fuse
# the words on either side ("hire C/F urgently" -> "hire urgently" is fine, but
# "pre-C/F-screened" would become "pre--screened"), and the model reads better
# prose when it can see that something was deliberately withheld than when it
# meets a sentence that silently lost a word.
REDACTION_MARKER = "[redacted]"


def redact(text: str, codes: list) -> tuple[str, list[str]]:
    """Remove every protected-attribute code from `text`.

    Args:
        text: One field of extracted opportunity text — title, description or
            requirements.
        codes: `OpportunityCode` rows (anything exposing `code` and `attribute`).

    Returns:
        The text with each protected code replaced by `REDACTION_MARKER`, and the
        verbatim strings that were removed, each listed once.
    """
    if not text or not codes:
        return text, []

    # Only a code carrying an `attribute` names a protected characteristic. A
    # code meaning "night shift" is a legitimate requirement the recruiter needs
    # matched, and removing it would quietly damage the search.
    protected: list[str] = []
    seen: set[str] = set()
    for entry in codes:
        code = (getattr(entry, "code", None) or "").strip()
        attribute = getattr(entry, "attribute", None)
        if not code or not attribute:
            continue
        # Two rows may record the same code in different spellings; report it once.
        if code.casefold() in seen:
            continue
        seen.add(code.casefold())
        protected.append(code)

    if not protected:
        return text, []

    # Longest first, so a code contained inside a longer one cannot chew a hole in
    # the middle of it and leave the more specific code half-standing.
    protected.sort(key=len, reverse=True)

    # Deliberately plain substring matching, with no word boundary: a code that
    # sits inside an ordinary word is redacted too. Over-redaction produces an
    # awkward sentence; under-redaction hands a protected characteristic to the
    # model. Only one of those is recoverable.
    #
    # One combined pass rather than one pass per code, so that text already
    # replaced can never be matched again — a two-letter code such as `ED` would
    # otherwise find itself inside a marker left by an earlier code and start
    # redacting the redactions.
    pattern = re.compile("|".join(re.escape(code) for code in protected), re.IGNORECASE)

    hits: set[str] = set()

    def _replace(match: re.Match) -> str:
        hits.add(match.group(0).casefold())
        return REDACTION_MARKER

    result = pattern.sub(_replace, text)

    # Report in each code's own spelling, once each.
    removed = [code for code in protected if code.casefold() in hits]
    return result, removed
