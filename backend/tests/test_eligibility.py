"""The eligibility service (§15) — the heart of this slice.

Pure-module tests, same style as `test_sourcing_redact.py`: no database, no
settings import, every regulatory bound passed in explicitly so a test is
never quietly checking against whatever `.env` happens to hold.
"""

from datetime import date

from app.services.sourcing import eligibility as elig

AS_OF = date(2026, 7, 30)
MIN_AGE = 23
MAX_AGE_EXCLUSIVE = 50
MIN_EDUCATION = 8
APPROVED = frozenset({"PH", "ID", "MM"})


def _facts(**overrides) -> elig.CandidateFacts:
    base = dict(sex=None, date_of_birth=None, education_years=None, nationality=None)
    base.update(overrides)
    return elig.CandidateFacts(**base)


def _mdw(facts: elig.CandidateFacts, **kw) -> list[elig.Finding]:
    return elig.evaluate(
        "mdw_work_permit",
        facts,
        as_of=AS_OF,
        min_age_years=MIN_AGE,
        max_age_years_exclusive=MAX_AGE_EXCLUSIVE,
        min_education_years=MIN_EDUCATION,
        approved_source_countries=APPROVED,
        **kw,
    )


def _by_criterion(findings: list[elig.Finding]) -> dict[str, elig.Finding]:
    return {f.criterion: f for f in findings}


# --- sex ---------------------------------------------------------------


def test_sex_met():
    f = _by_criterion(_mdw(_facts(sex="female")))[elig.CRITERION_SEX]
    assert f.outcome == elig.MET
    assert f.basis == elig.BASIS_REGULATORY


def test_sex_not_met():
    f = _by_criterion(_mdw(_facts(sex="male")))[elig.CRITERION_SEX]
    assert f.outcome == elig.NOT_MET


def test_sex_unknown_when_missing():
    f = _by_criterion(_mdw(_facts(sex=None)))[elig.CRITERION_SEX]
    assert f.outcome == elig.UNKNOWN
    assert "no sex recorded" in f.detail.lower()


# --- age -----------------------------------------------------------------


def test_age_met():
    f = _by_criterion(_mdw(_facts(date_of_birth=date(1990, 1, 1))))[elig.CRITERION_AGE]
    assert f.outcome == elig.MET


def test_age_not_met_too_young():
    f = _by_criterion(_mdw(_facts(date_of_birth=date(2010, 1, 1))))[elig.CRITERION_AGE]
    assert f.outcome == elig.NOT_MET


def test_age_not_met_too_old():
    f = _by_criterion(_mdw(_facts(date_of_birth=date(1960, 1, 1))))[elig.CRITERION_AGE]
    assert f.outcome == elig.NOT_MET


def test_age_unknown_when_missing():
    f = _by_criterion(_mdw(_facts(date_of_birth=None)))[elig.CRITERION_AGE]
    assert f.outcome == elig.UNKNOWN
    assert "no date of birth recorded" in f.detail.lower()


def test_turns_50_tomorrow_is_eligible_today():
    """The boundary, evaluated from the date: MOM's band is under 50, so a
    candidate who turns 50 tomorrow is still eligible today."""
    dob = date(AS_OF.year - MAX_AGE_EXCLUSIVE, AS_OF.month, AS_OF.day + 1)
    f = _by_criterion(_mdw(_facts(date_of_birth=dob)))[elig.CRITERION_AGE]
    assert f.outcome == elig.MET


def test_turned_50_yesterday_is_not_eligible():
    dob = date(AS_OF.year - MAX_AGE_EXCLUSIVE, AS_OF.month, AS_OF.day - 1)
    f = _by_criterion(_mdw(_facts(date_of_birth=dob)))[elig.CRITERION_AGE]
    assert f.outcome == elig.NOT_MET


def test_boundary_date_is_noted_when_near():
    dob = date(AS_OF.year - MAX_AGE_EXCLUSIVE, AS_OF.month, AS_OF.day + 1)
    f = _by_criterion(_mdw(_facts(date_of_birth=dob)))[elig.CRITERION_AGE]
    assert "turns 50" in f.detail.lower()


# --- education -------------------------------------------------------------


def test_education_met():
    f = _by_criterion(_mdw(_facts(education_years=8)))[elig.CRITERION_EDUCATION]
    assert f.outcome == elig.MET


def test_education_not_met():
    f = _by_criterion(_mdw(_facts(education_years=6)))[elig.CRITERION_EDUCATION]
    assert f.outcome == elig.NOT_MET


def test_education_unknown_when_missing():
    f = _by_criterion(_mdw(_facts(education_years=None)))[elig.CRITERION_EDUCATION]
    assert f.outcome == elig.UNKNOWN


# --- nationality -------------------------------------------------------------


def test_nationality_met():
    f = _by_criterion(_mdw(_facts(nationality="PH")))[elig.CRITERION_NATIONALITY]
    assert f.outcome == elig.MET
    # §15: this system's configuration, never MOM's policy verbatim.
    assert "configured approved-source list" in f.detail


def test_nationality_not_met():
    f = _by_criterion(_mdw(_facts(nationality="US")))[elig.CRITERION_NATIONALITY]
    assert f.outcome == elig.NOT_MET


def test_nationality_unknown_when_missing():
    f = _by_criterion(_mdw(_facts(nationality=None)))[elig.CRITERION_NATIONALITY]
    assert f.outcome == elig.UNKNOWN


# --- non-MDW placement types apply no MDW rules -----------------------------


def test_local_hire_regulatory_criteria_are_not_applicable():
    findings = elig.evaluate(
        "local_hire",
        _facts(),
        as_of=AS_OF,
        min_age_years=MIN_AGE,
        max_age_years_exclusive=MAX_AGE_EXCLUSIVE,
        min_education_years=MIN_EDUCATION,
        approved_source_countries=APPROVED,
    )
    by_criterion = _by_criterion(findings)
    for criterion in (
        elig.CRITERION_SEX,
        elig.CRITERION_AGE,
        elig.CRITERION_EDUCATION,
        elig.CRITERION_NATIONALITY,
    ):
        assert by_criterion[criterion].outcome == elig.NOT_APPLICABLE, criterion


def test_local_hire_with_no_facts_is_not_applicable_not_unknown():
    """A missing date of birth on a local hire is not a data gap to chase —
    the rule simply does not govern this placement."""
    findings = elig.evaluate(
        "local_hire",
        _facts(date_of_birth=None, education_years=None, nationality=None, sex=None),
        as_of=AS_OF,
        min_age_years=MIN_AGE,
        max_age_years_exclusive=MAX_AGE_EXCLUSIVE,
        min_education_years=MIN_EDUCATION,
        approved_source_countries=APPROVED,
    )
    outcomes = {
        f.outcome
        for f in findings
        if f.criterion != elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT
    }
    assert outcomes == {elig.NOT_APPLICABLE}


# --- every criterion always present, in stable order ------------------------


def test_every_criterion_is_always_present():
    findings = _mdw(_facts())
    criteria = [f.criterion for f in findings]
    assert criteria == [
        elig.CRITERION_SEX,
        elig.CRITERION_AGE,
        elig.CRITERION_EDUCATION,
        elig.CRITERION_NATIONALITY,
        elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT,
    ]


def test_order_is_stable_across_calls():
    a = [f.criterion for f in _mdw(_facts(sex="female"))]
    b = [f.criterion for f in _mdw(_facts(sex=None, nationality="PH"))]
    assert a == b


# --- occupational sex requirement -------------------------------------------


def test_occupational_requirement_met():
    f = _by_criterion(
        _mdw(_facts(sex="female"), sex_requirement="female", sex_requirement_reason="Elder care.")
    )[elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT]
    assert f.outcome == elig.MET
    assert f.basis == elig.BASIS_OCCUPATIONAL
    assert "Elder care." in f.detail


def test_occupational_requirement_not_met():
    f = _by_criterion(
        _mdw(_facts(sex="male"), sex_requirement="female", sex_requirement_reason="Elder care.")
    )[elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT]
    assert f.outcome == elig.NOT_MET
    assert "Elder care." in f.detail


def test_occupational_requirement_unknown_when_candidate_sex_missing():
    f = _by_criterion(
        _mdw(_facts(sex=None), sex_requirement="female", sex_requirement_reason="Elder care.")
    )[elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT]
    assert f.outcome == elig.UNKNOWN


def test_occupational_requirement_met_when_job_states_none():
    f = _by_criterion(_mdw(_facts(sex=None)))[elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT]
    assert f.outcome == elig.MET


def test_reason_carried_verbatim_not_paraphrased():
    reason = "Client requires a female helper for intimate personal care of an elderly resident."
    f = _by_criterion(
        _mdw(_facts(sex="female"), sex_requirement="female", sex_requirement_reason=reason)
    )[elig.CRITERION_OCCUPATIONAL_SEX_REQUIREMENT]
    assert reason in f.detail


# --- no boolean rollup -------------------------------------------------------


def test_no_boolean_rollup_in_response():
    """Findings only — see the module and endpoint docstrings for why an
    `eligible: true/false` field must never be added."""
    findings = _mdw(_facts(sex="female", date_of_birth=date(1990, 1, 1)))
    for f in findings:
        assert f.outcome in elig.OUTCOMES
    assert not hasattr(elig, "eligible")
