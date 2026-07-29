"""Tests for sourcing text comparison functions."""

from app.services.sourcing.text import overlap, salary_fit, tokens


class TestTokens:
    """Tokenization: lowercase, punctuation removed, whitespace split."""

    def test_simple_words(self):
        """Lowercase and split on whitespace."""
        assert tokens("Hello World") == {"hello", "world"}

    def test_punctuation_stripped(self):
        """Punctuation is removed."""
        assert tokens("Senior, Staff Nurse!") == {"senior", "staff", "nurse"}

    def test_case_insensitive(self):
        """Case is irrelevant."""
        assert tokens("SENIOR STAFF NURSE") == {"senior", "staff", "nurse"}

    def test_mixed_punctuation_and_case(self):
        """Combine punctuation removal and case conversion."""
        assert tokens("C++ Developer (Senior)") == {"c", "developer", "senior"}

    def test_empty_string(self):
        """Empty string yields empty set."""
        assert tokens("") == frozenset()

    def test_whitespace_only(self):
        """Whitespace-only string yields empty set."""
        assert tokens("   ") == frozenset()


class TestOverlap:
    """Overlap: containment of job tokens in candidate tokens."""

    def test_full_match_job_side(self):
        """Senior title fully covers less specific job requirement."""
        # Job asks for "Staff Nurse", candidate has "Senior Staff Nurse"
        # Job tokens: {staff, nurse}
        # Candidate tokens: {senior, staff, nurse}
        # Overlap: 2 / 2 = 1.0
        assert overlap("Staff Nurse", "Senior Staff Nurse") == 1.0

    def test_no_overlap(self):
        """Unrelated titles score near zero."""
        # Job: {python, developer}
        # Candidate: {accountant}
        # Overlap: 0 / 2 = 0.0
        assert overlap("Python Developer", "Accountant") == 0.0

    def test_partial_overlap(self):
        """Partial match reduces score."""
        # Job: {senior, developer}
        # Candidate: {senior, accountant}
        # Overlap: 1 / 2 = 0.5
        assert overlap("Senior Developer", "Senior Accountant") == 0.5

    def test_case_insensitive_overlap(self):
        """Case is irrelevant in matching."""
        assert overlap("Staff NURSE", "senior STAFF nurse") == 1.0

    def test_punctuation_irrelevant(self):
        """Punctuation is stripped before comparison."""
        assert overlap("C++ Developer", "Senior C++ Developer!") == 1.0

    def test_job_empty_returns_zero(self):
        """Empty job title returns 0.0."""
        assert overlap("", "Senior Staff Nurse") == 0.0

    def test_candidate_empty(self):
        """Candidate tokens don't have job tokens."""
        assert overlap("Staff Nurse", "") == 0.0

    def test_both_empty(self):
        """Both empty returns 0.0."""
        assert overlap("", "") == 0.0


class TestSalaryFit:
    """Salary fit: compare two salary bands with period normalization."""

    def test_same_currency_within_band(self):
        """Salary within band scores well (close to 1.0)."""
        # Candidate: 5,000 SGD/month
        # Job: 50,000–70,000 SGD/year = 4,166.67–5,833.33 SGD/month
        # Candidate is near the low end of the band
        result = salary_fit(5000, "SGD", "month", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result > 0.7  # Within band scores highly

    def test_different_currencies_returns_none(self):
        """Different currencies return None (no signal)."""
        result = salary_fit(5000, "SGD", "month", 50000, 70000, "USD", "year")
        assert result is None

    def test_candidate_missing_returns_none(self):
        """Missing candidate amount returns None."""
        result = salary_fit(None, "SGD", "month", 50000, 70000, "SGD", "year")
        assert result is None

    def test_job_min_missing_returns_none(self):
        """Missing job minimum returns None."""
        result = salary_fit(5000, "SGD", "month", None, 70000, "SGD", "year")
        assert result is None

    def test_job_max_missing_returns_none(self):
        """Missing job maximum returns None."""
        result = salary_fit(5000, "SGD", "month", 50000, None, "SGD", "year")
        assert result is None

    def test_candidate_period_missing_returns_none(self):
        """Missing candidate period returns None."""
        result = salary_fit(5000, "SGD", None, 50000, 70000, "SGD", "year")
        assert result is None

    def test_job_period_missing_returns_none(self):
        """Missing job period returns None."""
        result = salary_fit(5000, "SGD", "month", 50000, 70000, "SGD", None)
        assert result is None

    def test_above_band_scores_poorly(self):
        """Salary far above band scores poorly (close to 0.0)."""
        # Candidate: 100,000 SGD/month
        # Job: 50,000–70,000 SGD/year = 4,166–5,833 SGD/month
        # Candidate is far above the band
        result = salary_fit(100000, "SGD", "month", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result < 0.3  # Well above band scores poorly

    def test_below_band_scores_poorly(self):
        """Salary far below band scores poorly."""
        # Candidate: 1,000 SGD/month
        # Job: 50,000–70,000 SGD/year = 4,166–5,833 SGD/month
        result = salary_fit(1000, "SGD", "month", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result < 0.3  # Well below band scores poorly

    def test_period_normalization_hour_to_year(self):
        """Normalize hourly to annual."""
        # Candidate: 50 SGD/hour = 50 * 40 * 52 = 104,000 SGD/year
        # Job: 50,000–70,000 SGD/year
        # Candidate is above band
        result = salary_fit(50, "SGD", "hour", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result < 0.3  # Above band

    def test_period_normalization_day_to_year(self):
        """Normalize daily to annual."""
        # Candidate: 500 SGD/day = 500 * 240 = 120,000 SGD/year (assuming 240 working days)
        # Job: 50,000–70,000 SGD/year
        result = salary_fit(500, "SGD", "day", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result < 0.3  # Above band

    def test_period_normalization_week_to_year(self):
        """Normalize weekly to annual."""
        # Candidate: 2,000 SGD/week = 2,000 * 52 = 104,000 SGD/year
        # Job: 50,000–70,000 SGD/year
        result = salary_fit(2000, "SGD", "week", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result < 0.3  # Above band

    def test_same_period_no_conversion(self):
        """Same period, no conversion needed."""
        # Candidate: 60,000 SGD/year
        # Job: 50,000–70,000 SGD/year
        result = salary_fit(60000, "SGD", "year", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result > 0.7  # Within band

    def test_zero_candidate_amount_returns_score(self):
        """Zero amount is a valid (though bad) salary."""
        result = salary_fit(0, "SGD", "year", 50000, 70000, "SGD", "year")
        assert result is not None
        assert result < 0.1  # Well below band

    def test_zero_job_range_single_point(self):
        """Job min == job max is valid (single point)."""
        # Candidate: 60,000 SGD/year
        # Job: 60,000 SGD/year (min and max same)
        result = salary_fit(60000, "SGD", "year", 60000, 60000, "SGD", "year")
        assert result is not None
        assert result > 0.9  # Perfect match

    def test_inverted_job_range(self):
        """Job min > job max (should still work)."""
        # Candidate: 60,000 SGD/year
        # Job: 70,000–50,000 SGD/year (inverted, but still a band)
        result = salary_fit(60000, "SGD", "year", 70000, 50000, "SGD", "year")
        assert result is not None
        # Should handle it gracefully (normalize the range internally)
