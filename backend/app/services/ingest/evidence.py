"""Deterministic verification of what the model claimed (plan §14, §15).

The rule the product depends on — "nothing invented" — is a string comparison
here, not an instruction in a prompt. A prompt can be ignored by a model. A
comparison against the source cannot.

Nothing in this module repairs a bad claim. A quotation that is not in the
email leaves the value untrusted and demotes the extraction to review; it never
becomes `None` and never becomes a nearby guess. Silently correcting the model
is how a fabrication reaches the recruiter wearing the verifier's stamp of
approval.

The one thing that *is* corrected is where the quote sits, because that is not
a claim about the world — it is arithmetic, and see `locate` for why the model
is the wrong party to do it.
"""

import re
from functools import lru_cache

from app.core.config import settings
from app.services.ingest.schema import ExtractedField, ExtractedJob

_WHITESPACE = re.compile(r"\s+")
# The lookbehind keeps "$3,500.00" one amount rather than three: without it the
# fragment after each separator is matched again as its own figure, and a
# monthly wage becomes a range starting at 0. The trailing lookahead refuses a
# figure glued to a word ("3500pm"), because the suffix decides the magnitude
# and a suffix this module does not understand is a thousand-fold error waiting
# to be filed as a salary.
_AMOUNT = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?(?!\w)")
_SUFFIX_SCALE = {"k": 1_000.0, "m": 1_000_000.0}
# A reference number is a number in a salary sentence that is not money.
# "3500 (Ref #12345)" previously became a $12,345 ceiling.
_REFERENCE = re.compile(r"#|\bref(?:erence|s)?\b\.?", re.IGNORECASE)
# A cap is not an offer. "up to 3500" reported as 3500–3500 tells a recruiter
# the agency pays exactly that, which is a number the email never gave.
_CAP = re.compile(
    r"\b(up\s+to|upto|maximum|max|no\s+more\s+than|not\s+more\s+than|below|under)\b",
    re.IGNORECASE,
)
_FLOOR = re.compile(
    r"\b(from|starting(\s+(at|from))?|at\s+least|minimum|min|above|onwards?)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=8)
def _currency_codes(configured: str) -> tuple[str, ...]:
    return tuple(c.strip().upper() for c in configured.split(",") if c.strip())


@lru_cache(maxsize=8)
def _currency_symbols(configured: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for entry in configured.split(","):
        symbol, _, code = entry.partition("=")
        if symbol.strip() and code.strip():
            pairs.append((symbol.strip(), code.strip().upper()))
    # Longest first so "S$" is not read as the ambiguous bare "$".
    return tuple(sorted(pairs, key=lambda p: len(p[0]), reverse=True))


def _currency(raw: str) -> str | None:
    """Name a currency only when this deployment recognises the token.

    The previous `[A-Z]{3}` accepted any shouted word: "KLN pays 3500" filed
    salaries in currency KLN, and "OTE $90,000" in OTE. A wrong currency is
    worse than none — it survives into filters and comparisons looking like a
    fact somebody checked.
    """
    for symbol, code in _currency_symbols(settings.SALARY_CURRENCY_SYMBOLS):
        if symbol in raw:
            return code
    for match in re.finditer(r"\b([A-Za-z]{3})\b", raw):
        if match.group(1).upper() in _currency_codes(settings.SALARY_CURRENCY_CODES):
            return match.group(1).upper()
    return None


def _amounts(raw: str) -> list[float]:
    values = []
    for digits, suffix in _AMOUNT.findall(raw):
        value = float(digits.replace(",", ""))
        if suffix:
            value *= _SUFFIX_SCALE[suffix.lower()]
        values.append(value)
    return values


def _normalise(value: str) -> str:
    """Fold the differences that are formatting rather than content.

    HTML-to-text conversion and mail wrapping both change how much whitespace
    sits between two words. Failing an extraction over that would send correct
    answers to review, which trains reviewers to trust the flag less.
    """
    return _WHITESPACE.sub(" ", value).strip().lower()


@lru_cache(maxsize=4)
def _normalised_index(source: str) -> tuple[str, tuple[int, ...]]:
    """The normalised source, plus where each of its characters came from.

    Searching a normalised copy is easy; reporting an offset into the copy
    would be useless, because `extraction_evidence` has to point at characters
    a reviewer can actually find in the email. So the map is built alongside:
    position i of the normalised text came from `origin[i]` of the original.

    Lower-casing is done per character because a few characters lengthen when
    folded (Turkish dotted capital I becomes two). Emitting the same origin for
    each of the pieces keeps the map aligned instead of silently shifting every
    offset after the first such character.

    Cached because one source is asked about once per field, twelve times per
    vacancy, and again for every field when persist records validity.
    """
    out: list[str] = []
    origin: list[int] = []
    pending_space = False
    pending_at = 0
    for index, char in enumerate(source):
        if char.isspace():
            # A run of whitespace collapses to one space, and the space is
            # attributed to where the run started.
            if not pending_space:
                pending_space = True
                pending_at = index
            continue
        if pending_space:
            pending_space = False
            if out:
                # Leading whitespace is dropped rather than emitted: `strip()`
                # is part of the normalisation, and doing it here keeps the map
                # in step instead of having to shift it afterwards.
                out.append(" ")
                origin.append(pending_at)
        lowered = char.lower()
        out.extend(lowered)
        origin.extend([index] * len(lowered))
    # Trailing whitespace never got emitted, which is the `strip()` on that end.
    return "".join(out), tuple(origin)


def locate(field: ExtractedField, source: str) -> tuple[int, int] | None:
    """Find the field's quotation in the source, or say it is not there.

    This is the inversion the offset contract needed. Asking a model for
    character offsets asked it to do arithmetic over thousands of characters,
    which it does badly — a correct, verbatim quotation of a job description
    came back with offsets a few characters out and the whole extraction was
    rejected. Finding a string in a string is something code does exactly, so
    code does it.

    The model's offsets are still used, for the one thing they are good for:
    when a quote appears more than once — "Singapore" in a signature and in the
    location line — the occurrence nearest the claimed position is the one the
    model meant. Wrong offsets then cost a little precision about *which* copy,
    never the field itself.
    """
    quote = _normalise(field.evidence or "")
    if not quote:
        return None
    haystack, origin = _normalised_index(source)
    starts = []
    at = haystack.find(quote)
    while at != -1:
        starts.append(at)
        at = haystack.find(quote, at + 1)
    if not starts:
        return None
    spans = [(origin[s], origin[s + len(quote) - 1] + 1) for s in starts]
    if field.start_char is None:
        return spans[0]
    return min(spans, key=lambda span: abs(span[0] - field.start_char))


def verify(field: ExtractedField, source: str) -> bool:
    """Is the field's quotation really in the email, and does the value follow?

    A missing field is vacuously valid — there is nothing to locate. Everything
    else must be found in the source, and when it is, the offsets this module
    found are written back over whatever the model claimed. That write is the
    reason `start_char` in the database means something: it points at real
    characters, located here, rather than at the model's arithmetic.

    A quote that is nowhere in the email fails, and that is §15 intact — the
    only thing that ever proved a value was not invented was the quote being
    real, and that check is stricter here than it was, not weaker.
    """
    if field.is_missing:
        return True
    span = locate(field, source)
    if span is None:
        return False
    field.start_char, field.end_char = span
    return _value_is_corroborated(field)


def _value_is_corroborated(field: ExtractedField) -> bool:
    """Does the value the model wrote follow from the text it quoted?

    Matching the quote against the source only proves the quote is real. It
    proves nothing about `value`, which the model authors freely — so a field
    could quote "up to $3500" at correct offsets and record 9000, and every
    check in this module passed it. The quote is verified against the source,
    so the quote is the trustworthy half: any figure in the value that the
    quote does not contain is unsupported, and a salary is the single most
    damaging number this product can get wrong.

    Only numbers are policed. A value is legitimately a normalisation of its
    quote for text fields — "monthly" for "per month" — and demanding a
    substring there would send every correctly normalised field to review.
    """
    claimed = _amounts(field.value or "")
    if not claimed:
        return True
    quoted = _amounts(field.evidence or "")
    return all(value in quoted for value in claimed)


def parse_salary(raw: str) -> tuple[float | None, float | None, str | None]:
    """Pull a salary range out of a salary string, or refuse.

    Fails closed. Every branch that cannot account for what it read returns no
    figure, because `salary_raw` keeps the original text either way: a null
    range costs a recruiter one glance at the email, while a wrong one is acted
    on. The refusals each stand for a reproduced error — working hours read as
    a minimum, a reference number read as a maximum, "6k" read as six dollars.
    """
    if not raw:
        return None, None, None
    currency = _currency(raw)
    if _REFERENCE.search(raw):
        return None, None, currency
    amounts = _amounts(raw)
    if not amounts:
        return None, None, currency
    # A min/max explains two figures. More than that means the string holds
    # something this parser has not identified, and picking the extremes of a
    # set it does not understand is how "3500 for 40 hours" became a range.
    if len(amounts) > 2:
        return None, None, currency
    if any(
        value < settings.SALARY_MIN_CREDIBLE or value > settings.SALARY_MAX_CREDIBLE
        for value in amounts
    ):
        return None, None, currency
    low, high = min(amounts), max(amounts)
    if len(amounts) == 1:
        # A bound stays a bound. Collapsing it into low == high would state a
        # figure the email declined to commit to.
        if _CAP.search(raw):
            return None, high, currency
        if _FLOOR.search(raw):
            return low, None, currency
    return low, high, currency


# The salary period vocabulary. A CHECK constraint on `opportunities` and
# `candidates.salary_period` keys on these canonical words, and the salary sort
# already knows how to weigh one. Adding a period here means adding it to the
# CHECK constraint and to the sort's per-period factors in the same breath,
# which is why the vocabulary lives in code beside them rather than in settings
# — it is the shape of the data, not a setting anyone tunes.
_SALARY_PERIODS = ("hour", "day", "week", "month", "year")

# The words that do not contain the period they mean, so no amount of matching
# on the vocabulary above will find them.
#
# allow-hardcode: this is English irregularity, not a detect-list — "daily" is
# not spelled with "day" in it and "per annum" is not spelled with "year" in
# it. Everything regular ("monthly", "hourly", "per week") is derived from
# `_SALARY_PERIODS` rather than listed, so this stays short by construction
# instead of growing a line per phrasing a model happens to emit.
#
# Stems, not whole words, so one entry covers "annual", "annually", "annum"
# and "per annum". Both are long enough not to appear inside an unrelated
# word — which is why "p.a." is deliberately absent: a two-letter stem would
# match far more than it should, and an unread period is NULL, not a guess.
_IRREGULAR_PERIODS = {"dail": "day", "annu": "year"}

# Stems that contain a period but do not mean it. "biweekly" and "fortnightly"
# both carry "week", and reading either as a week values the salary at roughly
# twice what it pays — a wrong figure ranked confidently, which is worse than
# the missing one that a NULL produces. There is no `fortnight` in the
# vocabulary to map them onto, so they are refused outright.
_NOT_A_PERIOD = (
    "biweek",
    "fortnight",
    "bimonth",
    "semimonth",
    "semiannu",
    "biannu",
)


def parse_salary_period(raw: str | None) -> str | None:
    """The period as one of a known few, or nothing at all.

    A model asked for "month" will sometimes answer "Monthly" or "per month",
    and storing that verbatim is what made the salary sort drop those rows: it
    looked the period up in a table keyed on the canonical word and found
    nothing. Normalising here means the column holds one spelling of each
    period, which is what lets a CHECK constraint state that as a rule.

    An unrecognised word becomes NULL rather than a guess. That is the §15
    line — "the source said something we could not read" is not the same as
    "the source said monthly" — and nothing is lost by admitting it: the
    sentence the model read still sits in `salary_raw` / on the evidence row.

    Lives here, beside `parse_salary`, because both the email and CV pipelines
    need it — a salary figure is useless without its period, and reading one
    off a CV is the same word-matching problem as reading one off an email.
    """
    if raw is None:
        return None
    # Letters only, so "p.a.", "per-month" and "  Monthly " all reduce to the
    # word underneath the punctuation the model chose to decorate it with.
    cleaned = "".join(char for char in raw.lower() if char.isalpha())
    if not cleaned:
        return None
    if any(stem in cleaned for stem in _NOT_A_PERIOD):
        return None
    # Every period the answer mentions, not the first one found: "annual, paid
    # monthly" names two, and nothing here can say which one the figure is in.
    found = {period for period in _SALARY_PERIODS if period in cleaned}
    found |= {
        period for irregular, period in _IRREGULAR_PERIODS.items()
        if irregular in cleaned
    }
    if len(found) == 1:
        return found.pop()
    return None


def quality_state(job: ExtractedJob, source: str) -> str:
    """Combine deterministic checks with the model's own confidence.

    Deterministic signals dominate on purpose: a self-reported 0.99 is not a
    calibrated probability, and must never outvote a span that does not exist.
    Confidence only ever demotes — it can turn `verified` into `likely`, never
    the reverse.
    """
    fields = [f for f in vars(job).values() if isinstance(f, ExtractedField)]
    present = [f for f in fields if not f.is_missing]

    # An extraction that found nothing is a failure, not a confident empty
    # answer. Left as `verified` it would sit in the dashboard as a real
    # vacancy with every column blank.
    if not present:
        return "needs_review"
    if any(not verify(f, source) for f in present):
        return "needs_review"

    if job.salary is not None and not job.salary.is_missing:
        # Read the figures out of the evidence, never out of `value`. The
        # evidence has been checked against the source; `value` has not, and
        # trusting it here is what let a model-authored 9000 be recorded as a
        # verified salary for an email that said $3500.
        low, high, _currency = parse_salary(job.salary.evidence or "")
        # Verified evidence for a salary that carries no figure still yields
        # nothing an `opportunities` row can store or filter on.
        if low is None and high is None:
            return "needs_review"
        if job.salary_period is None or job.salary_period.is_missing:
            # An amount with no period is not comparable to any other amount,
            # which makes every salary analytic built on it wrong. $3500 is a
            # good monthly wage and an insulting annual one.
            return "likely"

    # The weakest field sets the verdict: a row is only as trustworthy as the
    # column a recruiter is about to act on, and averaging would let eleven
    # easy fields hide the one that was guessed.
    weakest = min(f.confidence for f in present)
    return (
        "verified" if weakest >= settings.EXTRACTION_VERIFIED_CONFIDENCE else "likely"
    )
