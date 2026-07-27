"""Deterministic verification of what the model claimed (plan §14, §15).

The rule the product depends on — "nothing invented" — is a string comparison
here, not an instruction in a prompt. A prompt can be ignored by a model. A
comparison against the source cannot.

Nothing in this module repairs a bad claim. A span that does not hold what the
model said it holds leaves the value untrusted and demotes the extraction to
review; it never becomes `None` and never becomes a nearby guess. Silently
correcting the model is how a fabrication reaches the recruiter wearing the
verifier's stamp of approval.
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


def verify(field: ExtractedField, source: str) -> bool:
    """Does the claimed span actually contain the claimed evidence?

    A missing field is vacuously valid — there is nothing to locate. Everything
    else must match the source at the offsets it named. Deliberately exact
    (modulo whitespace): models miscount characters often, so near-misses are
    common, but searching nearby for the quote would mean the offsets prove
    nothing about where the value came from — which is the entire reason the
    contract asks for them.
    """
    if field.is_missing:
        return True
    if field.start_char is None or field.end_char is None:
        return False
    # Python slices out of range rather than raising, so an offset past the end
    # would otherwise compare an empty string — indistinguishable from a field
    # that quoted nothing at all.
    if field.start_char < 0 or field.end_char > len(source):
        return False
    if not field.evidence:
        return False
    if _normalise(source[field.start_char : field.end_char]) != _normalise(
        field.evidence
    ):
        return False
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
