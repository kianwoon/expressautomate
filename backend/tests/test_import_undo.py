# allow-hardcode: the names, employers, addresses and phone numbers below are
# test fixture content, not a matching or scoring oracle.
"""Walking an import back, and refusing to walk back what is no longer its doing.

The import writes to live data with no preview, so undo is the whole safety
net under it — and an undo that reached past a recruiter's later correction
would destroy exactly the value somebody cared enough to fix. The two
sequences the rule exists for are walked explicitly below: an edit made after
the import survives, and an import applied twice undoes the same way both
times.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import select, text

from app.db.rls import tenant_session
from app.models.candidate import (
    Candidate,
    CandidateFieldOverride,
    CandidateImport,
    CandidateImportChange,
    CandidateRole,
)
from app.services.imports.apply import apply_import
from app.services.imports.rows import CandidateRecord, RoleRecord
from app.services.imports.undo import undo_import
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import agency, other_agency  # noqa: F401

TODAY = date(2026, 7, 29)


async def _an_import(tenant_id: uuid.UUID, state: str = CandidateImport.DONE) -> uuid.UUID:
    import_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_imports (id, tenant_id, filename, content_type,"
                " byte_size, object_key, state)"
                " VALUES (:i, :t, 'roster.xlsx', 'application/vnd.ms-excel', 10, :k, :s)"
            ),
            {
                "i": import_id,
                "t": tenant_id,
                "k": f"{tenant_id}/imports/{import_id}.xlsx",
                "s": state,
            },
        )
        await s.commit()
    return import_id


def _candidate(**overrides) -> CandidateRecord:
    fields = {
        "line": 2,
        "full_name": "Jane Tan",
        "email": "jane@acme.sg",
        "phone_raw": None,
        "phone_e164": None,
        "current_title": "Staff Nurse",
        "current_employer": "Parkway Shenton",
        "location": "Singapore",
    }
    fields.update(overrides)
    return CandidateRecord(**fields)


def _role(**overrides) -> RoleRecord:
    fields = {
        "line": 2,
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
        "current_title": None,
        "location": None,
    }
    values.update(columns)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email,"
                " current_title, location, pipeline_stage, record_status)"
                " VALUES (:i, :t, :n, :e, :ct, :loc, 'new', 'active')"
            ),
            {
                "i": candidate_id,
                "t": tenant_id,
                "n": values["full_name"],
                "e": values["email"],
                "ct": values["current_title"],
                "loc": values["location"],
            },
        )
        await s.commit()
    return candidate_id


async def _apply(tenant_id, import_id, candidates, roles=()):
    async with tenant_session(tenant_id) as session:
        return await apply_import(
            session,
            tenant_id=tenant_id,
            import_id=import_id,
            candidates=list(candidates),
            roles=list(roles),
            today=TODAY,
        )


async def _undo(tenant_id, import_id):
    async with tenant_session(tenant_id) as session:
        return await undo_import(session, tenant_id=tenant_id, import_id=import_id)


async def _one_candidate(tenant_id) -> Candidate:
    async with tenant_session(tenant_id) as session:
        return (await session.execute(select(Candidate))).scalars().one()


async def _hand_written_change(
    tenant_id, import_id, *, entity_type, entity_id, field_name, previous, new
) -> None:
    """A change row written directly, for cases `apply_import` cannot stage.

    Restoring a date or a blanked field needs a specific before/after pair on
    an existing row, and driving `apply_import` into producing one would mean
    contorting the matcher instead of testing undo.
    """
    async with AdminSessionLocal() as s:
        s.add(
            CandidateImportChange(
                tenant_id=tenant_id,
                import_id=import_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=CandidateImportChange.UPDATED,
                field_name=field_name,
                previous_value=previous,
                new_value=new,
            )
        )
        await s.commit()


# The rule -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edit_made_after_the_import_survives_the_undo(agency):  # noqa: F811
    """A→B by the import, B→C by a recruiter: undo leaves C alone and says so."""
    tenant_id, _user = agency
    candidate_id = await _existing_candidate(tenant_id, full_name="Jane T")
    import_id = await _an_import(tenant_id)

    await _apply(tenant_id, import_id, [_candidate(full_name="Jane Tan")])

    async with tenant_session(tenant_id) as session:
        candidate = (
            (await session.execute(select(Candidate).where(Candidate.id == candidate_id)))
            .scalars()
            .one()
        )
        assert candidate.full_name == "Jane Tan"
        candidate.full_name = "Jane Tan-Lim"

    outcome = await _undo(tenant_id, import_id)

    assert outcome.fields_skipped >= 1
    skipped = {skip.field_name for skip in outcome.skips}
    assert "full_name" in skipped
    reason = next(skip.reason for skip in outcome.skips if skip.field_name == "full_name")
    assert "Jane Tan-Lim" in reason and "Jane Tan" in reason

    candidate = await _one_candidate(tenant_id)
    assert candidate.full_name == "Jane Tan-Lim"
    # Everything the recruiter did not retype still came back.
    assert candidate.current_title is None
    assert candidate.location is None


@pytest.mark.asyncio
async def test_the_same_import_applied_twice_undoes_the_same_way_both_times(agency):  # noqa: F811
    """A→B, undo→A, A→B again, undo→A. The second run is not a special case."""
    tenant_id, _user = agency
    await _existing_candidate(tenant_id, full_name="Jane T", location="Johor")

    first_import = await _an_import(tenant_id)
    await _apply(tenant_id, first_import, [_candidate()])
    assert (await _one_candidate(tenant_id)).full_name == "Jane Tan"

    first_undo = await _undo(tenant_id, first_import)
    candidate = await _one_candidate(tenant_id)
    assert candidate.full_name == "Jane T"
    assert candidate.location == "Johor"

    second_import = await _an_import(tenant_id)
    await _apply(tenant_id, second_import, [_candidate()])
    assert (await _one_candidate(tenant_id)).full_name == "Jane Tan"

    second_undo = await _undo(tenant_id, second_import)
    candidate = await _one_candidate(tenant_id)
    assert candidate.full_name == "Jane T"
    assert candidate.location == "Johor"

    assert first_undo.fields_restored == second_undo.fields_restored
    assert first_undo.fields_skipped == second_undo.fields_skipped == 0
    assert first_undo.rows_deleted == second_undo.rows_deleted == 0


@pytest.mark.asyncio
async def test_undoing_twice_in_a_row_is_harmless(agency):  # noqa: F811
    """The second pass finds nothing still matching, so it changes nothing."""
    tenant_id, _user = agency
    await _existing_candidate(tenant_id, full_name="Jane T")
    import_id = await _an_import(tenant_id)
    await _apply(tenant_id, import_id, [_candidate()])

    first = await _undo(tenant_id, import_id)
    assert first.fields_restored > 0
    before = await _one_candidate(tenant_id)

    second = await _undo(tenant_id, import_id)
    assert second.fields_restored == 0
    assert second.rows_deleted == 0

    after = await _one_candidate(tenant_id)
    assert after.full_name == before.full_name == "Jane T"
    assert after.current_title == before.current_title is None
    assert after.location == before.location is None


# Created rows -------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_deletes_the_rows_the_import_created(agency):  # noqa: F811
    """A person and a job that exist only because of this import both go."""
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)
    outcome = await _apply(tenant_id, import_id, [_candidate()], [_role()])
    assert outcome.candidates_created == 1
    assert outcome.roles_created == 1

    undone = await _undo(tenant_id, import_id)
    assert undone.rows_deleted == 2
    assert undone.fields_skipped == 0

    async with tenant_session(tenant_id) as session:
        assert (await session.execute(select(Candidate))).scalars().all() == []
        assert (await session.execute(select(CandidateRole))).scalars().all() == []


@pytest.mark.asyncio
async def test_undo_of_a_created_row_is_not_repeated_on_a_second_pass(agency):  # noqa: F811
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)
    await _apply(tenant_id, import_id, [_candidate()], [_role()])

    assert (await _undo(tenant_id, import_id)).rows_deleted == 2
    assert (await _undo(tenant_id, import_id)).rows_deleted == 0


# Values undo has to put back in their own type ----------------------------


@pytest.mark.asyncio
async def test_a_date_the_import_rewrote_comes_back_as_a_date(agency):  # noqa: F811
    """`previous_value` is text in the log and a `date` on the column."""
    tenant_id, _user = agency
    created_import = await _an_import(tenant_id)
    await _apply(tenant_id, created_import, [_candidate()], [_role()])

    async with tenant_session(tenant_id) as session:
        role = (await session.execute(select(CandidateRole))).scalars().one()
        role_id = role.id
        assert role.started_on == date(2019, 3, 1)

    later_import = await _an_import(tenant_id)
    await _hand_written_change(
        tenant_id,
        later_import,
        entity_type=CandidateImportChange.ROLE,
        entity_id=role_id,
        field_name="started_on",
        previous="2018-06-01",
        new="2019-03-01",
    )

    outcome = await _undo(tenant_id, later_import)
    assert outcome.fields_restored == 1

    async with tenant_session(tenant_id) as session:
        role = (await session.execute(select(CandidateRole))).scalars().one()
        assert role.started_on == date(2018, 6, 1)


@pytest.mark.asyncio
async def test_a_field_the_import_emptied_comes_back(agency):  # noqa: F811
    """NULL on the current side is a value undo compares like any other."""
    tenant_id, _user = agency
    candidate_id = await _existing_candidate(tenant_id, location=None)
    import_id = await _an_import(tenant_id)
    await _hand_written_change(
        tenant_id,
        import_id,
        entity_type=CandidateImportChange.CANDIDATE,
        entity_id=candidate_id,
        field_name="location",
        previous="Singapore",
        new=None,
    )

    outcome = await _undo(tenant_id, import_id)
    assert outcome.fields_restored == 1
    assert (await _one_candidate(tenant_id)).location == "Singapore"


# Refusals -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_refuses_while_the_import_is_still_parsing(agency):  # noqa: F811
    """Reversing a run still in flight would race the run itself."""
    tenant_id, _user = agency
    await _existing_candidate(tenant_id, full_name="Jane T")
    import_id = await _an_import(tenant_id, state=CandidateImport.PARSING)
    await _apply(tenant_id, import_id, [_candidate()])

    with pytest.raises(ValueError, match="parsing"):
        await _undo(tenant_id, import_id)

    assert (await _one_candidate(tenant_id)).full_name == "Jane Tan"


@pytest.mark.asyncio
async def test_undo_refuses_another_agencys_import(agency, other_agency):  # noqa: F811
    """Nothing in the schema stops `import_id` crossing tenants, so this does."""
    tenant_id, _user = agency
    other_tenant, _other = other_agency
    theirs = await _an_import(other_tenant)
    their_candidate = await _existing_candidate(other_tenant, full_name="Jane T")
    await _apply(other_tenant, theirs, [_candidate()])

    with pytest.raises(ValueError):
        await _undo(tenant_id, theirs)

    async with tenant_session(other_tenant) as session:
        candidate = (
            (await session.execute(select(Candidate).where(Candidate.id == their_candidate)))
            .scalars()
            .one()
        )
        assert candidate.full_name == "Jane Tan"


@pytest.mark.asyncio
async def test_a_field_a_human_had_overridden_is_never_touched(agency):  # noqa: F811
    """The import never wrote it, so undo has nothing to put back over it."""
    tenant_id, _user = agency
    candidate_id = await _existing_candidate(
        tenant_id, full_name="Jane T", current_title="Nurse Manager"
    )
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

    import_id = await _an_import(tenant_id)
    await _apply(tenant_id, import_id, [_candidate()])

    outcome = await _undo(tenant_id, import_id)
    assert "current_title" not in {skip.field_name for skip in outcome.skips}

    candidate = await _one_candidate(tenant_id)
    assert candidate.current_title == "Nurse Manager"
    assert candidate.full_name == "Jane T"


# State --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_import_is_marked_undone(agency):  # noqa: F811
    tenant_id, _user = agency
    import_id = await _an_import(tenant_id)
    await _apply(tenant_id, import_id, [_candidate()])

    await _undo(tenant_id, import_id)

    async with tenant_session(tenant_id) as session:
        record = (
            (await session.execute(select(CandidateImport).where(CandidateImport.id == import_id)))
            .scalars()
            .one()
        )
        assert record.state == CandidateImport.UNDONE
        # The change log survives the reversal: it is the evidence that the
        # import, and its undo, ever happened.
        changes = (
            (
                await session.execute(
                    select(CandidateImportChange).where(
                        CandidateImportChange.import_id == import_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert changes != []
