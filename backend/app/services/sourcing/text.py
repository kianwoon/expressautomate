"""Text comparison and salary scoring for candidate sourcing.

Pure module — no database, no settings, no I/O.
"""

import string
from decimal import Decimal

# Period normalization factors to annual basis.
# These assume a standard employment model: 40-hour work week, 8-hour work day,
# 5-day work week, 52 weeks per year.
_PERIOD_TO_ANNUAL = {
    "hour": 40 * 52,  # 2080 hours/year
    "day": 40 * 52 / 8,  # 260 days/year (40h/week ÷ 8h/day)
    "week": 52,
    "month": 12,
    "year": 1,
}


def tokens(value: str) -> frozenset[str]:
    """Tokenize a string: lowercase, strip punctuation, split on whitespace.

    Returns a frozenset of tokens. An empty input or whitespace-only input
    yields an empty set.

    Args:
        value: String to tokenize (e.g., "Senior Staff Nurse").

    Returns:
        Frozenset of lowercase tokens with punctuation removed.
    """
    if not value or not value.strip():
        return frozenset()

    # Remove punctuation and convert to lowercase.
    cleaned = value.translate(str.maketrans("", "", string.punctuation)).lower()

    # Split on whitespace and filter out empty strings.
    return frozenset(t for t in cleaned.split() if t)


def overlap(job_title: str, candidate_title: str) -> float:
    """Score title similarity using containment.

    Returns the fraction of the job title's tokens that appear in the candidate's
    title. This is a containment metric, not Jaccard: "Senior Staff Nurse" against
    a job asking for "Staff Nurse" scores as a full match (1.0) because the candidate
    has both required tokens, even though Jaccard would penalize the candidate for
    being more specific. Containment reflects what a recruiter expects: a more
    qualified candidate is a better fit, not a worse one.

    Args:
        job_title: The job vacancy title.
        candidate_title: The candidate's title.

    Returns:
        Float between 0.0 and 1.0; 0.0 when the job title is empty.
    """
    job_tokens = tokens(job_title)
    if not job_tokens:
        return 0.0

    candidate_tokens = tokens(candidate_title)

    # Containment: how many of the job's tokens appear in the candidate's tokens.
    shared = len(job_tokens & candidate_tokens)
    return shared / len(job_tokens)


def salary_fit(
    candidate_amount: float | Decimal | None,
    candidate_currency: str | None,
    candidate_period: str | None,
    job_min: float | Decimal | None,
    job_max: float | Decimal | None,
    job_currency: str | None,
    job_period: str | None,
) -> float | None:
    """Score how well a candidate's salary expectation fits a job's band.

    Returns None (no signal) when the two currencies differ, or when either side
    has missing data. A missing signal and a bad fit are different facts; conflating
    them would mark a candidate down for something nobody recorded.

    Periods are normalized to an annual basis before comparison, so a candidate
    expecting 6,000 monthly against a job paying 90,000 annually is correctly
    identified as a good fit (6,000 × 12 = 72,000, within or near a ~90k band).

    No currency conversion is performed. A rate we did not fetch on a date we did
    not record is a fabricated fact.

    The amounts may arrive as Decimal (the ORM reads Numeric columns as Decimal)
    or float. They are coerced to float once, at the top: the band arithmetic
    mixes them with float literals, and `1.0 - Decimal` raises TypeError — the
    crash that took down the first production run whose salary actually scored
    (every earlier run abstained on a missing currency before reaching here).

    Args:
        candidate_amount: Candidate's salary expectation (or None).
        candidate_currency: Currency code (e.g., "SGD", "USD").
        candidate_period: Frequency (hour|day|week|month|year).
        job_min: Job's salary band minimum.
        job_max: Job's salary band maximum.
        job_currency: Job's currency code.
        job_period: Job's frequency (hour|day|week|month|year).

    Returns:
        Float between 0.0 and 1.0 when all inputs are present and currencies match;
        None otherwise.
    """
    # Check for missing data.
    if (
        candidate_amount is None
        or candidate_currency is None
        or candidate_period is None
        or job_min is None
        or job_max is None
        or job_currency is None
        or job_period is None
    ):
        return None

    # Check for currency mismatch.
    if candidate_currency != job_currency:
        return None

    # Coerce to float now that the values are known present. See the docstring:
    # the arithmetic below is float math, and a Decimal operand would raise.
    candidate_amount = float(candidate_amount)
    job_min = float(job_min)
    job_max = float(job_max)

    # Normalize both sides to annual basis.
    candidate_annual = candidate_amount * _PERIOD_TO_ANNUAL[candidate_period]
    job_min_annual = job_min * _PERIOD_TO_ANNUAL[job_period]
    job_max_annual = job_max * _PERIOD_TO_ANNUAL[job_period]

    # Ensure job band is in proper order (handle inverted ranges gracefully).
    if job_min_annual > job_max_annual:
        job_min_annual, job_max_annual = job_max_annual, job_min_annual

    band_width = job_max_annual - job_min_annual

    if band_width == 0:
        # Single point: score based on distance from the point.
        if candidate_annual == job_min_annual:
            return 1.0
        distance = abs(candidate_annual - job_min_annual)
        # Use relative distance from the single point.
        return max(0.0, 1.0 - distance / max(job_min_annual, 1.0))

    # Band width > 0: score based on position relative to band.
    if job_min_annual <= candidate_annual <= job_max_annual:
        # Within band: score is perfect.
        return 1.0

    # Outside band: score decreases with distance.
    if candidate_annual < job_min_annual:
        distance = job_min_annual - candidate_annual
    else:
        distance = candidate_annual - job_max_annual

    # Linear decay: score = 1 - (distance / band_width), clamped to [0, 1].
    return max(0.0, 1.0 - distance / band_width)
