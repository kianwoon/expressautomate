# allow-hardcode: the names, employers, addresses and phone numbers below are
# test fixture content, not a matching or scoring oracle.
"""Applying parsed spreadsheet rows: what is matched, what is written, what is refused.

Every rule the brief pins down has a test here, because each of them is a
decision an implementer would otherwise make differently and silently: a
conflict is never resolved, a human's edit always outranks the sheet, a
history row never invents a person, and one person listed twice stays one
person. The last test is the one that catches drift — re-applying the same
file must change nothing and duplicate nothing.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import select, text

from app.db.rls import tenant_session
from app.models.candidate import (
    Candidate,
    CandidateFieldOverride,
    CandidateImportChange,
    CandidateRole,
)
from app.services.imports.apply import apply_import
from app.services.imports.rows import CandidateRecord, RoleRecord
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import agency, other_agency  # noqa: F401

TODAY = date(2026, 7, 29)


async def _an_import(tenant_id: uuid.UUID) -> uuid.UUID:
    import_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_imports (id, tenant_id, filename, content_type,"
                " byte_size, object_key, state)"
                " VALUES (:i, :t, 'roster.xlsx', 'application/vnd.ms-excel', 10, :k, 'parsing')"
            ),
            {"i": import_id, "t": tenant_id, "k": f"{tenant_id}/imports/{import_id}.xlsx"},
        )
        await s.commit()
    return import_id


def _candidate(**overrides) -> CandidateRecord:
    fields = {
        "full_name": "Jane Tan",
        "email": "jane@acme.sg",
        "phone_raw": "+65 9123 4567",
        "phone_e164": "+6591234567",
        "current_title": "Staff Nurse",
        "current_employer": "Parkway Shenton",
        "location": "Singapore",
    }
    fields.update(overrides)
    return CandidateRecord(**fields)


def _role(**overrides) -> RoleRecord:
    fields = {
        "candidate_email": "jane@acme.sg",
        "candidate_phone": None,
        "employer": "Parkway Shenton",
        "title": "Staff Nurse",
        "started_on": date(2019, 3, 1),
        "started_precision": "month",
        "ended_on": date(2020, 3, 1),
        "ended_precision": "month",
        "location": "Singapore",
        "description": "Ward rounds.",
    }
    fields.update(overrides)
    return RoleRecord(**fields)


async def _existing_candidate(tenant_id, **columns) -> uuid.UUID:
    candidate_id = uuid.uuid4()
    values = {
        "full_name": "Jane T",
        "email": "jane@acme.sg",
        "phone_e164": None,
        "current_title": None,
        "location": None,
    }
    values.update(columns)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email, phone_e164,"
                " current_title, location, pipeline_stage, record_status)"
                " VALUES (:i, :t, :n, :e, :p, :ct, :loc, 'new', 'active')"
            ),
            {
                "i": candidate_id,
                "t": tenant_id,
                "n": values["full_name"],
                "e": values["email"],
                "p": values["phone_e164"],
                "ct": values["current_title"],
                "loc": values["location"],
            },
        )
        await s.commit()
    return candidate_id


# Rule 1 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_conflict_writes_nothing_and_names_both_people(agency):  # noqa: F811
    """Email points at one person and phone at another: neither is touched."""
    tenant_id, _user = agency
    by_email = await _existing_candidate(tenant_id, email="jane@acme.sg", phone_e164=None)
    by_phone = await _existing_candidate(
        tenant_id, full_name="Janet Ong", email="janet@acme.sg", phone_e164="+6591234567"
    )
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate()],
            roles=[],
            today=TODAY,
        )

    assert outcome.candidates_created == 0
    assert outcome.candidates_updated == 0
    assert len(outcome.problems) == 1
    reason = outcome.problems[0].reason
    assert str(by_email) in reason and str(by_phone) in reason

    async with tenant_session(tenant_id) as session:
        names = set(
            (await session.execute(select(Candidate.full_name))).scalars().all()
        )
        assert names == {"Jane T", "Janet Ong"}
        assert (await session.execute(select(CandidateImportChange))).scalars().all() == []


# Rule 2 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_import_wins_except_on_a_field_a_person_edited(agency):  # noqa: F811
    """`current_title` was asserted by a human, so only the name moves."""
    tenant_id, _user = agency
    candidate_id = await _existing_candidate(
        tenant_id, full_name="Jane T", current_title="Nurse Manager"
    )
    import_id = await _an_import(tenant_id)
    async with AdminSessionLocal() as s:
        s.add(
            CandidateFieldOverride(
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                field_name="current_title",
                human_value="Nurse Manager",
            )
        )
        await s.commit()

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate(phone_raw=None, phone_e164=None)],
            roles=[],
            today=TODAY,
        )

    assert outcome.candidates_updated == 1
    assert outcome.problems == []

    async with tenant_session(tenant_id) as session:
        candidate = (await session.execute(select(Candidate))).scalars().one()
        assert candidate.full_name == "Jane Tan"
        assert candidate.current_title == "Nurse Manager"

        changes = (
            (await session.execute(select(CandidateImportChange))).scalars().all()
        )
        by_field = {c.field_name: c for c in changes}
        assert "current_title" not in by_field
        assert by_field["full_name"].previous_value == "Jane T"
        assert by_field["full_name"].new_value == "Jane Tan"
        assert by_field["full_name"].action == CandidateImportChange.UPDATED
        assert by_field["full_name"].entity_type == CandidateImportChange.CANDIDATE
        assert by_field["full_name"].entity_id == candidate.id


# Rule 3 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_created_candidate_carries_the_import_and_a_created_change(agency):  # noqa: F811
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate()],
            roles=[],
            today=TODAY,
        )

    assert outcome.candidates_created == 1

    async with tenant_session(tenant_id) as session:
        candidate = (await session.execute(select(Candidate))).scalars().one()
        assert candidate.import_id == import_id
        assert candidate.full_name == "Jane Tan"
        change = (
            (await session.execute(select(CandidateImportChange))).scalars().one()
        )
        assert change.action == CandidateImportChange.CREATED
        assert change.entity_id == candidate.id
        assert change.previous_value is None


# Rule 4 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_import_from_another_tenant_is_refused(agency, other_agency):  # noqa: F811
    """No composite FK stops this at the database, so the code has to."""
    tenant_id, _user = agency
    other_tenant, _other_user = other_agency
    foreign_import = await _an_import(other_tenant)

    with pytest.raises(ValueError):
        async with tenant_session(tenant_id) as session:
            await apply_import(
                session,
                tenant_id=tenant_id,
                import_id=foreign_import,
                candidates=[_candidate()],
                roles=[],
                today=TODAY,
            )

    async with tenant_session(tenant_id) as session:
        assert (await session.execute(select(Candidate))).scalars().all() == []


# Rule 5 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_history_row_attaches_to_a_person_created_in_the_same_file(agency):  # noqa: F811
    """Candidates are applied and flushed before any role is looked at."""
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate()],
            roles=[_role()],
            today=TODAY,
        )

    assert outcome.candidates_created == 1
    assert outcome.roles_created == 1
    assert outcome.problems == []

    async with tenant_session(tenant_id) as session:
        candidate = (await session.execute(select(Candidate))).scalars().one()
        role = (await session.execute(select(CandidateRole))).scalars().one()
        assert role.candidate_id == candidate.id
        assert role.import_id == import_id


# Rule 6 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_person_twice_in_one_file_is_one_person(agency):  # noqa: F811
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate(), _candidate()],
            roles=[],
            today=TODAY,
        )

    assert outcome.candidates_created == 1
    assert outcome.candidates_updated == 0
    assert outcome.problems == []

    async with tenant_session(tenant_id) as session:
        assert len((await session.execute(select(Candidate))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_two_rows_for_one_person_that_disagree_are_reported(agency):  # noqa: F811
    """The second row is refused by name, rather than quietly winning."""
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate(), _candidate(full_name="Jane Tang")],
            roles=[],
            today=TODAY,
        )

    assert outcome.candidates_created == 1
    assert len(outcome.problems) == 1
    reason = outcome.problems[0].reason
    assert "full_name" in reason and "Jane Tan" in reason and "Jane Tang" in reason

    async with tenant_session(tenant_id) as session:
        candidate = (await session.execute(select(Candidate))).scalars().one()
        assert candidate.full_name == "Jane Tan"


# Rule 7 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_history_row_matching_nobody_creates_nobody(agency):  # noqa: F811
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[],
            roles=[_role(candidate_email="nobody@acme.sg")],
            today=TODAY,
        )

    assert outcome.candidates_created == 0
    assert outcome.roles_created == 0
    assert len(outcome.problems) == 1
    assert "nobody@acme.sg" in outcome.problems[0].reason

    async with tenant_session(tenant_id) as session:
        assert (await session.execute(select(Candidate))).scalars().all() == []
        assert (await session.execute(select(CandidateRole))).scalars().all() == []


# Rule 8 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_role_that_matches_an_existing_one_updates_it(agency):  # noqa: F811
    """Same employer, overlapping span: one role, updated, not a second row."""
    tenant_id, _user = agency
    candidate_id = await _existing_candidate(tenant_id)
    import_id = await _an_import(tenant_id)
    role_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision, ended_on, ended_precision, source, status)"
                " VALUES (:i, :t, :c, 'Parkway Shenton', 'parkway shenton', 'Nurse',"
                " 'nurse', '2019-06-01', 'month', '2020-01-01', 'month', 'human',"
                " 'confirmed')"
            ),
            {"i": role_id, "t": tenant_id, "c": candidate_id},
        )
        await s.commit()

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[],
            roles=[_role()],
            today=TODAY,
        )

    assert outcome.roles_updated == 1
    assert outcome.roles_created == 0

    async with tenant_session(tenant_id) as session:
        role = (await session.execute(select(CandidateRole))).scalars().one()
        assert role.id == role_id
        assert role.title == "Staff Nurse"
        assert role.source == "import"
        assert role.status == CandidateRole.CONFIRMED
        titles = [
            c
            for c in (
                await session.execute(select(CandidateImportChange))
            ).scalars().all()
            if c.field_name == "title"
        ]
        assert titles[0].previous_value == "Nurse"
        assert titles[0].new_value == "Staff Nurse"


@pytest.mark.asyncio
async def test_a_role_matching_nothing_is_created_confirmed_from_the_import(agency):  # noqa: F811
    tenant_id, _user = agency
    await _existing_candidate(tenant_id)
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[],
            roles=[_role()],
            today=TODAY,
        )

    assert outcome.roles_created == 1

    async with tenant_session(tenant_id) as session:
        role = (await session.execute(select(CandidateRole))).scalars().one()
        assert role.source == "import"
        assert role.status == CandidateRole.CONFIRMED
        assert role.import_id == import_id


# Rule 9 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_date_with_no_precision_is_dropped_and_the_role_kept(agency):  # noqa: F811
    """Task 4 hands over an unreadable date as `None`; the job still counts."""
    tenant_id, _user = agency
    await _existing_candidate(tenant_id)
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[],
            roles=[
                _role(
                    started_on=None,
                    started_precision=None,
                    ended_on=date(2020, 3, 1),
                    ended_precision=None,
                )
            ],
            today=TODAY,
        )

    assert outcome.roles_created == 1

    async with tenant_session(tenant_id) as session:
        role = (await session.execute(select(CandidateRole))).scalars().one()
        assert role.started_on is None
        assert role.ended_on is None
        assert role.title == "Staff Nurse"


# Re-applying ------------------------------------------------------------


@pytest.mark.asyncio
async def test_re_applying_the_same_records_changes_and_duplicates_nothing(agency):  # noqa: F811
    tenant_id, _user = agency
    first_import = await _an_import(tenant_id)
    records = [_candidate()]
    histories = [_role()]

    async with tenant_session(tenant_id) as session:
        first = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=first_import,
            candidates=records,
            roles=histories,
            today=TODAY,
        )
    assert (first.candidates_created, first.roles_created) == (1, 1)

    second_import = await _an_import(tenant_id)
    async with tenant_session(tenant_id) as session:
        second = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=second_import,
            candidates=records,
            roles=histories,
            today=TODAY,
        )

    assert second.candidates_created == 0
    assert second.roles_created == 0
    assert second.problems == []

    async with tenant_session(tenant_id) as session:
        assert len((await session.execute(select(Candidate))).scalars().all()) == 1
        assert len((await session.execute(select(CandidateRole))).scalars().all()) == 1
        # Nothing actually differed the second time, so the second import
        # recorded no field changes at all — which is what makes its undo a
        # no-op rather than a rewrite.
        second_changes = (
            (
                await session.execute(
                    select(CandidateImportChange).where(
                        CandidateImportChange.import_id == second_import
                    )
                )
            )
            .scalars()
            .all()
        )
        assert second_changes == []


@pytest.mark.asyncio
async def test_an_import_never_reaches_another_agencys_candidate(agency, other_agency):  # noqa: F811
    """The same email in two agencies is two people, and B's row is untouched."""
    tenant_id, _user = agency
    other_tenant, _other = other_agency
    theirs = await _existing_candidate(other_tenant, full_name="Jane T")
    import_id = await _an_import(tenant_id)

    async with tenant_session(tenant_id) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=[_candidate()],
            roles=[],
            today=TODAY,
        )

    assert outcome.candidates_created == 1

    async with tenant_session(other_tenant) as session:
        candidate = (
            (await session.execute(select(Candidate).where(Candidate.id == theirs)))
            .scalars()
            .one()
        )
        assert candidate.full_name == "Jane T"
        assert candidate.import_id is None
