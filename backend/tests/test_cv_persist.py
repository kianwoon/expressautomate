# allow-hardcode: employer names, titles and CV prose below are test fixture
# content, not a matching or scoring oracle.
"""Storing what a CV said, and deciding when it said something already known.

The matching rule is the part of this an implementer would otherwise guess at,
so every clause of it has a test: month-pinned comparison, half-open overlap,
open-ended roles, and the deliberate refusal to guess when a parsed role
carries no dates and two existing roles share its employer.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.models.candidate import CandidateDocument
from app.services.cv.persist import match_existing_role, persist_cv, stored_date
from app.services.cv.schema import CVResponse, ExtractedDate
from app.services.llm.client import LLMResult
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import _a_candidate_row, agency  # noqa: F401

CV = (
    "Jane Tan\n"
    "Staff Nurse, Parkway Shenton, Mar 2019 to Mar 2020\n"
    "Senior Nurse, Raffles Medical, Mar 2020 to present\n"
    "Skills: triage, phlebotomy\n"
    "Last drawn salary: S$2,500/month\n"
    "Expected salary: $3,000/month\n"
)


def _salary_field(amount, currency, period, evidence):
    """An ExtractedSalary-shaped dict for test payloads."""
    return {
        "amount": amount,
        "currency": currency,
        "period": period,
        "evidence": evidence,
        "confidence": 0.9,
    }

TODAY = date(2026, 7, 29)


class _Row:
    """An existing role, shaped as the matcher reads one back from the table."""

    def __init__(self, employer_normalized, started_on=None, started_precision=None,
                 ended_on=None, ended_precision=None, status="unconfirmed"):
        self.id = uuid.uuid4()
        self.employer_normalized = employer_normalized
        self.started_on = started_on
        self.started_precision = started_precision
        self.ended_on = ended_on
        self.ended_precision = ended_precision
        self.status = status


def _field(value, evidence=None, confidence=0.9):
    at = CV.find(evidence or value)
    return {
        "value": value,
        "evidence": evidence or value,
        "start_char": at,
        "end_char": at + len(evidence or value),
        "confidence": confidence,
    }


def _date(value, evidence, precision):
    return {**_field(value, evidence), "precision": precision}


def _role(title, company, start=None, end=None):
    role = {"title": _field(title), "company": _field(company)}
    if start:
        role["start_date"] = _date(*start)
    if end:
        role["end_date"] = _date(*end)
    return role


def _response(roles=(), skills=(), last_drawn=None, expected=None) -> tuple[CVResponse, LLMResult]:
    payload = {"roles": list(roles), "skills": [_field(s) for s in skills]}
    if last_drawn:
        payload["last_drawn_salary"] = last_drawn
    if expected:
        payload["expected_salary"] = expected
    return CVResponse.model_validate(payload), LLMResult(
        data=payload, model="test/fast", latency_ms=12, raw={"choices": []}
    )


# --- the matching rule, as arithmetic -----------------------------------


def test_a_month_off_a_cv_matches_a_typed_day_in_the_same_month():
    """"Mar 2019" and 2019-03-14 describe the same month, so one role."""
    existing = [_Row("parkway shenton", date(2019, 3, 14), "day", date(2020, 3, 2), "day")]
    assert (
        match_existing_role(
            "parkway shenton", date(2019, 3, 1), "month", date(2020, 3, 1), "month",
            existing, TODAY,
        )
        == existing[0].id
    )


def test_adjacent_roles_do_not_overlap():
    """One ends March 2020, the next starts March 2020. Two roles, not one."""
    existing = [_Row("acme", date(2018, 1, 1), "month", date(2020, 3, 1), "month")]
    assert (
        match_existing_role(
            "acme", date(2020, 3, 1), "month", date(2021, 1, 1), "month", existing, TODAY
        )
        is None
    )


def test_an_open_ended_role_runs_to_today_on_both_sides():
    existing = [_Row("raffles medical", date(2020, 3, 1), "month", None, None)]
    assert (
        match_existing_role(
            "raffles medical", date(2024, 1, 1), "month", None, None, existing, TODAY
        )
        == existing[0].id
    )


def test_an_undated_role_matches_one_employer_and_refuses_two():
    one = [_Row("acme", date(2019, 1, 1), "year")]
    assert match_existing_role("acme", None, None, None, None, one, TODAY) == one[0].id

    two = [*one, _Row("acme", date(2022, 1, 1), "year")]
    # Two rows and nothing to tell them apart: picking one would be a guess
    # recorded as a fact, so the parsed role is inserted unconfirmed instead.
    assert match_existing_role("acme", None, None, None, None, two, TODAY) is None


def test_a_same_month_role_matches_a_same_month_role():
    """A role that starts and ends in the same month is not zero-length
    for matching purposes, or it could never match anything."""
    existing = [_Row("acme", date(2019, 3, 1), "month", date(2019, 3, 1), "month")]
    assert (
        match_existing_role(
            "acme", date(2019, 3, 1), "month", date(2019, 3, 1), "month", existing, TODAY
        )
        == existing[0].id
    )


def test_a_same_month_role_does_not_match_a_different_month():
    existing = [_Row("acme", date(2019, 3, 1), "month", date(2019, 3, 1), "month")]
    assert (
        match_existing_role(
            "acme", date(2019, 4, 1), "month", date(2019, 4, 1), "month", existing, TODAY
        )
        is None
    )


def test_a_return_to_a_former_employer_is_a_second_role():
    existing = [_Row("acme", date(2015, 1, 1), "month", date(2017, 1, 1), "month")]
    assert (
        match_existing_role(
            "acme", date(2021, 1, 1), "month", date(2023, 1, 1), "month", existing, TODAY
        )
        is None
    )


def test_an_undated_role_ignores_a_rejected_row_when_picking_the_one_live_match():
    """A rejected role at the same employer is not a candidate for
    corroboration and must not make an otherwise-unambiguous live match
    look ambiguous."""
    live = _Row("acme", date(2019, 1, 1), "year", status="unconfirmed")
    rejected = _Row("acme", date(2020, 1, 1), "year", status="rejected")
    assert match_existing_role("acme", None, None, None, None, [live, rejected], TODAY) == live.id
    assert match_existing_role("acme", None, None, None, None, [rejected, live], TODAY) == live.id


def test_an_overlapping_live_role_wins_over_an_overlapping_rejected_role():
    """Same input, both orderings: the live role must win every time, since
    `_EXISTING_ROLES` carries no `ORDER BY` to rely on."""
    live = _Row("acme", date(2019, 1, 1), "month", date(2020, 1, 1), "month", status="unconfirmed")
    rejected = _Row("acme", date(2019, 1, 1), "month", date(2020, 1, 1), "month", status="rejected")
    for existing in ([live, rejected], [rejected, live]):
        assert (
            match_existing_role(
                "acme", date(2019, 6, 1), "month", date(2019, 9, 1), "month", existing, TODAY
            )
            == live.id
        )


def test_a_role_matching_only_a_rejected_role_still_matches_it():
    """`a57e4f0`'s behaviour: with no live match, a rejected row is still
    found, purely so the caller can suppress the insert."""
    rejected = _Row("acme", date(2019, 1, 1), "month", date(2020, 1, 1), "month", status="rejected")
    assert (
        match_existing_role(
            "acme", date(2019, 6, 1), "month", date(2019, 9, 1), "month", [rejected], TODAY
        )
        == rejected.id
    )


# --- date precision ------------------------------------------------------


def test_a_month_never_becomes_a_day():
    day, precision = stored_date(
        ExtractedDate.model_validate(_date("Mar 2019", "Mar 2019", "month"))
    )
    assert (day, precision) == (date(2019, 3, 1), "month")


def test_a_year_stays_a_year():
    day, precision = stored_date(
        ExtractedDate.model_validate(_date("2019", "2019", "year"))
    )
    assert (day, precision) == (date(2019, 1, 1), "year")


def test_a_day_is_kept_whole():
    value = _date("3 March 2019", "3 March 2019", "day")
    day, precision = stored_date(ExtractedDate.model_validate(value))
    assert (day, precision) == (date(2019, 3, 3), "day")


def test_an_ambiguous_numeric_date_is_not_guessed_at():
    """3/4/2019: both fields are <=12, so the CV does not say which is the
    day and which is the month. Storing 3 April would assert a fact the CV
    never stated (§15); only the year is honest."""
    value = _date("3/4/2019", "3/4/2019", "day")
    day, precision = stored_date(ExtractedDate.model_validate(value))
    assert (day, precision) == (date(2019, 1, 1), "year")


def test_an_unambiguous_numeric_date_still_reads_day_first():
    value = _date("25/4/2019", "25/4/2019", "day")
    day, precision = stored_date(ExtractedDate.model_validate(value))
    assert (day, precision) == (date(2019, 4, 25), "day")


# --- what actually lands in the tables -----------------------------------


async def _document(tenant_id, candidate_id) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_documents (id, tenant_id, candidate_id, filename,"
                " content_type, byte_size, object_key, parse_state)"
                " VALUES (:i, :t, :c, 'cv.pdf', 'application/pdf', 10, :k, 'parsing')"
            ),
            {"i": document_id, "t": tenant_id, "c": candidate_id, "k": f"{tenant_id}/cv.pdf"},
        )
        await s.commit()
    return document_id


async def _cleanup(tenant_id):
    async with AdminSessionLocal() as s:
        for table in ("extraction_evidence", "extractions", "candidate_documents"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()


async def _run(tenant_id, candidate_id, document_id, response, result, today=None):
    async with tenant_session(tenant_id) as session:
        document = await session.get(CandidateDocument, document_id)
        kwargs = {"today": today} if today is not None else {}
        await persist_cv(
            session,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            document=document,
            response=response,
            result=result,
            text=CV,
            **kwargs,
        )


@pytest.mark.asyncio
async def test_a_new_role_arrives_as_an_unconfirmed_proposal(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    response, result = _response(
        roles=[_role("Staff Nurse", "Parkway Shenton",
                     ("Mar 2019", "Mar 2019", "month"),
                     ("Mar 2020", "Mar 2020", "month"))],
        skills=["triage"],
    )

    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        role = (
            await s.execute(
                text(
                    "SELECT source, status, extraction_id, started_on, started_precision"
                    " FROM candidate_roles WHERE candidate_id = :c"
                ),
                {"c": candidate_id},
            )
        ).one()
        assert role.source == "cv_upload"
        assert role.status == "unconfirmed"
        assert role.extraction_id is not None
        assert (role.started_on, role.started_precision) == (date(2019, 3, 1), "month")

        document = (
            await s.execute(
                text("SELECT parse_state, dropped_count FROM candidate_documents WHERE id = :i"),
                {"i": document_id},
            )
        ).one()
        assert (document.parse_state, document.dropped_count) == ("parsed", 0)
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_skill_already_held_is_not_duplicated(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_skills (id, tenant_id, candidate_id, skill,"
                " skill_normalized, source, status)"
                " VALUES (:i, :t, :c, 'Triage', 'triage', 'human', 'confirmed')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    response, result = _response(skills=["triage", "phlebotomy"])
    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT skill_normalized, source, status FROM candidate_skills"
                    " WHERE candidate_id = :c ORDER BY skill_normalized"
                ),
                {"c": candidate_id},
            )
        ).all()
    assert [r.skill_normalized for r in rows] == ["phlebotomy", "triage"]
    # The human's row is left exactly as it was, not demoted to a proposal.
    assert [(r.source, r.status) for r in rows if r.skill_normalized == "triage"] == [
        ("human", "confirmed")
    ]
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_an_extraction_that_found_nothing_is_a_visible_state(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)

    await _run(tenant_id, candidate_id, document_id, *_response())

    async with AdminSessionLocal() as s:
        state = (
            await s.execute(
                text("SELECT parse_state FROM candidate_documents WHERE id = :i"),
                {"i": document_id},
            )
        ).scalar_one()
    assert state == "empty"
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_what_verification_dropped_is_recorded_on_the_document(agency):  # noqa: F811
    """The model answered for two roles; only one quoted the page.

    `extract_cv` discards the other silently, which is right for the data and
    wrong for the person reading it — hence the count and the sentence.
    """
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)

    response, result = _response(roles=[_role("Staff Nurse", "Parkway Shenton")])
    # The raw answer the model gave, before verification took one role and one
    # skill out of it.
    result.data["roles"].append(_role("Chief Executive", "Nowhere Ltd"))
    result.data["skills"].append(_field("neurosurgery"))

    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT parse_state, dropped_count, dropped_reason"
                    " FROM candidate_documents WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one()
    assert row.parse_state == "parsed"
    assert row.dropped_count == 2
    assert "1 role(s)" in row.dropped_reason and "1 skill(s)" in row.dropped_reason
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_blank_skill_is_counted_as_dropped(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)

    # A blank `value` still needs a real quotation to pass `ExtractedField`'s
    # anti-fabrication check, so the blank skill quotes real CV text — the
    # blankness under test is in `value` alone, same as a model that echoed
    # whitespace for a bullet with nothing legible in it.
    payload = {
        "roles": [],
        "skills": [_field("triage"), _field("   ", evidence="triage")],
    }
    response = CVResponse.model_validate(payload)
    result = LLMResult(data=payload, model="test/fast", latency_ms=12, raw={"choices": []})
    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT dropped_count FROM candidate_documents WHERE id = :i"),
                {"i": document_id},
            )
        ).one()
    assert row.dropped_count == 1
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_today_is_injectable_for_the_open_ended_role_path(agency):  # noqa: F811
    """`today` defaults to the real clock but can be pinned, matching
    `candidate_tenure.derive(roles, today=...)`, so an open-ended role's
    match against "today" is testable without waiting on the calendar."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    # allow-hardcode: an INSERT statement (test fixture setup), not a
    # detect-list or matching oracle.
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision, ended_on, ended_precision, source, status)"
                " VALUES (:i, :t, :c, 'Raffles Medical', 'raffles medical', 'Senior Nurse',"
                " 'senior nurse', '2020-03-01', 'month', NULL, NULL, 'human', 'confirmed')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    response, result = _response(
        roles=[_role("Senior Nurse", "Raffles Medical",
                     ("Mar 2020", "Mar 2020", "month"))]
    )
    await _run(tenant_id, candidate_id, document_id, response, result, today=date(2030, 1, 1))

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text("SELECT source FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).all()
    # Matched against the existing open-ended role rather than inserted anew.
    assert [r.source for r in rows] == ["human"]
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_role_the_candidate_already_has_is_not_inserted_again(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision, ended_on, ended_precision, source, status)"
                " VALUES (:i, :t, :c, 'Parkway Shenton', 'parkway shenton', 'Staff Nurse',"
                " 'staff nurse', '2019-03-14', 'day', '2020-03-02', 'day', 'human',"
                " 'confirmed')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    response, result = _response(
        roles=[_role("Staff Nurse", "Parkway Shenton",
                     ("Mar 2019", "Mar 2019", "month"),
                     ("Mar 2020", "Mar 2020", "month"))]
    )
    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text("SELECT source, status FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).all()
        evidence = (
            await s.execute(
                text(
                    "SELECT count(*) FROM extraction_evidence"
                    " WHERE tenant_id = :t AND candidate_role_id IS NOT NULL"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert [(r.source, r.status) for r in rows] == [("human", "confirmed")]
    # The row was recognised, not rewritten — but what the CV said about it is
    # still attached to it.
    assert evidence > 0
    await _cleanup(tenant_id)


# allow-hardcode: the INSERT statements below are test fixture setup rows
# (shaping data for the scenario under test), not a detect-list or oracle.
@pytest.mark.asyncio
async def test_a_rejected_role_is_not_reinserted(agency):  # noqa: F811
    """A recruiter rejected this role. The reject endpoint's own docstring
    promises a re-parse will not bring it back — so a parsed role matching it
    must create no row, and the rejected row must stay rejected."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision, ended_on, ended_precision, source, status)"
                " VALUES (:i, :t, :c, 'Parkway Shenton', 'parkway shenton', 'Staff Nurse',"
                " 'staff nurse', '2019-03-14', 'day', '2020-03-02', 'day', 'human',"
                " 'rejected')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    response, result = _response(
        roles=[_role("Staff Nurse", "Parkway Shenton",
                     ("Mar 2019", "Mar 2019", "month"),
                     ("Mar 2020", "Mar 2020", "month"))]
    )
    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text("SELECT source, status FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).all()
        # Not attached to the rejected row: it should not silently accumulate
        # corroboration it will never show.
        attached = (
            await s.execute(
                text(
                    "SELECT count(*) FROM extraction_evidence"
                    " WHERE tenant_id = :t AND candidate_role_id IS NOT NULL"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
        # But not invisible either: the match is still on the extraction
        # record, just pointed at no role.
        unattached = (
            await s.execute(
                text(
                    "SELECT count(*) FROM extraction_evidence"
                    " WHERE tenant_id = :t AND candidate_role_id IS NULL"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert [(r.source, r.status) for r in rows] == [("human", "rejected")]
    assert attached == 0
    assert unattached > 0
    await _cleanup(tenant_id)


# allow-hardcode: the INSERT statement below is test fixture setup, not a
# detect-list or oracle.
@pytest.mark.asyncio
async def test_reuploading_the_same_cv_does_not_resurrect_a_rejected_role(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision, ended_on, ended_precision, source, status)"
                " VALUES (:i, :t, :c, 'Parkway Shenton', 'parkway shenton', 'Staff Nurse',"
                " 'staff nurse', '2019-03-14', 'day', '2020-03-02', 'day', 'human',"
                " 'rejected')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    response, result = _response(
        roles=[_role("Staff Nurse", "Parkway Shenton",
                     ("Mar 2019", "Mar 2019", "month"),
                     ("Mar 2020", "Mar 2020", "month"))]
    )
    # Two uploads of the same CV, same as a recruiter re-parsing it by mistake.
    document_id_2 = await _document(tenant_id, candidate_id)
    await _run(tenant_id, candidate_id, document_id, response, result)
    await _run(tenant_id, candidate_id, document_id_2, response, result)

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text("SELECT source, status FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).all()
    assert [(r.source, r.status) for r in rows] == [("human", "rejected")]
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_second_stint_at_the_same_employer_is_a_second_role(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision, ended_on, ended_precision, source, status)"
                " VALUES (:i, :t, :c, 'Parkway Shenton', 'parkway shenton', 'Staff Nurse',"
                " 'staff nurse', '2015-01-01', 'year', '2017-01-01', 'year', 'human',"
                " 'confirmed')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    response, result = _response(
        roles=[_role("Staff Nurse", "Parkway Shenton",
                     ("Mar 2019", "Mar 2019", "month"),
                     ("Mar 2020", "Mar 2020", "month"))]
    )
    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT source FROM candidate_roles WHERE candidate_id = :c"
                    " ORDER BY started_on"
                ),
                {"c": candidate_id},
            )
        ).all()
    assert [r.source for r in rows] == ["human", "cv_upload"]
    await _cleanup(tenant_id)


# --- salary extraction ---------------------------------------------------


@pytest.mark.asyncio
async def test_salary_quoted_on_the_cv_lands_on_the_candidate(agency):  # noqa: F811
    """Both salary fields the CV states are parsed and written."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    response, result = _response(
        last_drawn=_salary_field(2500.0, "SGD", "month", "S$2,500/month"),
        expected=_salary_field(3000.0, "SGD", "month", "$3,000/month"),
    )

    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT last_drawn_salary, last_drawn_currency, last_drawn_period,"
                    " expected_salary, salary_period"
                    " FROM candidates WHERE id = :c"
                ),
                {"c": candidate_id},
            )
        ).one()
    assert float(row.last_drawn_salary) == 2500.0
    assert row.last_drawn_currency == "SGD"
    assert row.last_drawn_period == "month"
    assert float(row.expected_salary) == 3000.0
    assert row.salary_period == "month"
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_human_salary_value_is_not_overwritten_by_the_cv(agency):  # noqa: F811
    """`COALESCE` means a recruiter's figure always wins over the CV's."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE candidates SET last_drawn_salary = 9999.0,"
                " expected_salary = 8888.0 WHERE id = :c"
            ),
            {"c": candidate_id},
        )
        await s.commit()

    response, result = _response(
        last_drawn=_salary_field(2500.0, "SGD", "month", "S$2,500/month"),
        expected=_salary_field(3000.0, "SGD", "month", "$3,000/month"),
    )
    await _run(tenant_id, candidate_id, document_id, response, result)

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT last_drawn_salary, expected_salary"
                    " FROM candidates WHERE id = :c"
                ),
                {"c": candidate_id},
            )
        ).one()
    assert float(row.last_drawn_salary) == 9999.0
    assert float(row.expected_salary) == 8888.0
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_cv_with_no_salary_leaves_the_columns_null(agency):  # noqa: F811
    """A CV that states no salary must not produce fabricated figures."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _document(tenant_id, candidate_id)

    await _run(tenant_id, candidate_id, document_id, *_response())

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT last_drawn_salary, expected_salary"
                    " FROM candidates WHERE id = :c"
                ),
                {"c": candidate_id},
            )
        ).one()
    assert row.last_drawn_salary is None
    assert row.expected_salary is None
    await _cleanup(tenant_id)
