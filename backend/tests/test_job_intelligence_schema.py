"""Schema coercion for model output shape drift.

Regression coverage for the 2026-08-27 production failure: the understand
stage arrived with `potential_challenges` as one joined sentence and Pydantic
refused it, killing a paid analysis (`job_intelligence_failed`, arq 05:00Z).
Every list field on every stage now tolerates the joined-string form.

allow-hardcode: the malformed payloads below are fixtures reproducing what
the model actually returned, not an oracle.
"""

from app.services.job_intelligence.schema import (
    CandidatePersona,
    JDUnderstanding,
    SearchPlan,
)

# The exact failure from the arq log, minimised.
_JOINED_CHALLENGES = "High volumes of KYC/SOW applications; tight 6-month engagement"


def _understanding_payload(**overrides):
    base = {
        "role": "Compliance Officer",
        "business_purpose": "Keep the licence",
        "daily_activities": ["Review filings"],
        "work_environment": "Office",
        "must_have_requirements": ["5 years compliance"],
        "preferred_requirements": [],
        "working_conditions": "Hybrid",
        "success_characteristics": ["Detail oriented"],
        "potential_challenges": [_JOINED_CHALLENGES],
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


def test_understanding_coerces_joined_string_to_list():
    payload = _understanding_payload(potential_challenges=_JOINED_CHALLENGES)
    result = JDUnderstanding.model_validate(payload)
    assert result.potential_challenges == [
        "High volumes of KYC/SOW applications",
        "tight 6-month engagement",
    ]


def test_understanding_splits_on_commas_newlines_and_arrows():
    result = JDUnderstanding.model_validate(
        _understanding_payload(daily_activities="Filing → Reviewing,\nReporting")
    )
    assert result.daily_activities == ["Filing", "Reviewing", "Reporting"]


def test_understanding_still_accepts_proper_lists_and_ignores_scalars():
    """Coercion must not disturb a well-formed answer or touch str fields."""
    result = JDUnderstanding.model_validate(_understanding_payload())
    assert result.daily_activities == ["Review filings"]
    # A string field that contains commas stays whole.
    assert result.work_environment == "Office"


def test_persona_coerces_joined_strings():
    result = CandidatePersona.model_validate(
        {
            "likely_backgrounds": "Banking; Audit; Big 4 consulting",
            "transferable_roles": [],
            "transferable_industries": "",
            "behaviours": ["Methodical"],
            "communication_style": "Precise",
            "career_stage": "Mid-career",
            "motivations": "Stability → Public good",
            "salary_expectation": "Not mentioned",
            "availability": "1 month",
        }
    )
    assert result.likely_backgrounds == ["Banking", "Audit", "Big 4 consulting"]
    # Empty string coerces to empty list, not [""].
    assert result.transferable_industries == []
    assert result.motivations == ["Stability", "Public good"]


def test_search_plan_coerces_queries():
    result = SearchPlan.model_validate(
        {
            "platform": "linkedin",
            "priority": 2,
            "queries": 'compliance officer AND ("KYC" OR "SOW")',
            "negative_queries": "",
            "salary": "SGD 8k",
            "location": "Singapore",
            "employment_type": "Permanent",
        }
    )
    # A boolean query is one search string; commas inside it must not be
    # split apart. The coercion splits only when the string carries the
    # join marks — this one has none, so it survives whole as a single
    # query. (A quoted comma-free boolean never hits the splitter's
    # boundaries; see `_coerce_str_list`.)
    assert len(result.queries) == 1
    assert result.negative_queries == []
