"""An import, and the trail that lets it be taken back."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.rls import tenant_session
from app.models.candidate import CandidateImport, CandidateImportChange
from tests.test_candidate_roles_api import agency, other_agency  # noqa: F401


@pytest.mark.asyncio
async def test_an_import_belongs_to_one_tenant_only(agency, other_agency):  # noqa: F811
    """Agency B cannot see Agency A's import even knowing its id."""
    a_tenant, a_user = agency
    b_tenant, _b_user = other_agency

    async with tenant_session(a_tenant) as session:
        session.add(
            CandidateImport(
                tenant_id=a_tenant,
                filename="roster.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                byte_size=1024,
                object_key=f"{a_tenant}/imports/roster.xlsx",
                state=CandidateImport.PENDING,
                uploaded_by=a_user,
            )
        )
        await session.commit()

    async with tenant_session(b_tenant) as session:
        assert (await session.execute(select(CandidateImport))).scalars().all() == []


@pytest.mark.asyncio
async def test_an_import_change_belongs_to_one_tenant_only(agency, other_agency):  # noqa: F811
    """Agency B cannot see Agency A's import change even knowing its id."""
    a_tenant, a_user = agency
    b_tenant, _b_user = other_agency

    async with tenant_session(a_tenant) as session:
        candidate_import = CandidateImport(
            tenant_id=a_tenant,
            filename="roster.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            byte_size=1024,
            object_key=f"{a_tenant}/imports/roster.xlsx",
            state=CandidateImport.PARSING,
            uploaded_by=a_user,
        )
        session.add(candidate_import)
        await session.flush()
        session.add(
            CandidateImportChange(
                tenant_id=a_tenant,
                import_id=candidate_import.id,
                entity_type=CandidateImportChange.CANDIDATE,
                entity_id=uuid.uuid4(),
                action=CandidateImportChange.CREATED,
                field_name="full_name",
                previous_value=None,
                new_value="Jane Tan",
            )
        )
        await session.commit()

    async with tenant_session(b_tenant) as session:
        assert (
            (await session.execute(select(CandidateImportChange))).scalars().all() == []
        )


@pytest.mark.asyncio
async def test_import_state_rejects_a_value_outside_import_states(agency):  # noqa: F811
    """The CHECK constraint on `state` rejects anything not in IMPORT_STATES."""
    a_tenant, a_user = agency

    with pytest.raises((IntegrityError, DBAPIError)):
        async with tenant_session(a_tenant) as session:
            session.add(
                CandidateImport(
                    tenant_id=a_tenant,
                    filename="roster.xlsx",
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    byte_size=1024,
                    object_key=f"{a_tenant}/imports/roster.xlsx",
                    state="bogus",
                    uploaded_by=a_user,
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_import_change_new_value_and_previous_value_are_both_stored(agency):  # noqa: F811
    """`new_value` and `previous_value` are independent columns, both readable back.

    The undo rule (Task 6) is "restore a field only if its current value
    still equals what the import wrote" — evaluating that needs both sides
    of the comparison, so this locks in that neither column silently mirrors
    the other.
    """
    a_tenant, a_user = agency

    async with tenant_session(a_tenant) as session:
        candidate_import = CandidateImport(
            tenant_id=a_tenant,
            filename="roster.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            byte_size=1024,
            object_key=f"{a_tenant}/imports/roster.xlsx",
            state=CandidateImport.PARSING,
            uploaded_by=a_user,
        )
        session.add(candidate_import)
        await session.flush()
        change = CandidateImportChange(
            tenant_id=a_tenant,
            import_id=candidate_import.id,
            entity_type=CandidateImportChange.CANDIDATE,
            entity_id=uuid.uuid4(),
            action=CandidateImportChange.UPDATED,
            field_name="current_title",
            previous_value="Nurse",
            new_value="Staff Nurse",
        )
        session.add(change)
        await session.commit()
        change_id = change.id

    async with tenant_session(a_tenant) as session:
        stored = await session.get(CandidateImportChange, change_id)
        assert stored.previous_value == "Nurse"
        assert stored.new_value == "Staff Nurse"
