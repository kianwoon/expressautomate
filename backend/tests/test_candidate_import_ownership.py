"""An import is a bulk create and a bulk edit, so it meets both boundaries."""

import pytest
from sqlalchemy import text

from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_an_import_owns_the_rows_it_creates(admin_session, seeded, run_import) -> None:
    """Somebody uploading their own contact list does not intend to donate it
    to the shared queue."""
    make_tenant, _, _ = seeded
    tenant_id, importer, _ = await make_tenant("agency-import-owner")
    await admin_session.commit()

    outcome = await run_import(
        tenant_id, importer, [{"full_name": "New Person", "email": "n@x.com"}]
    )
    assert outcome.candidates_created == 1

    row = (
        await admin_session.execute(
            text("SELECT owner_id FROM candidates WHERE email = 'n@x.com'")
        )
    ).one()
    assert row.owner_id == importer


@pytest.mark.asyncio
async def test_an_import_skips_a_row_a_colleague_holds(
    admin_session, seeded, run_import
) -> None:
    """Both cases read the same to the importer, and are worded the same:
    invisible, and visible-but-shared. They may edit neither."""
    make_tenant, _, _ = seeded
    tenant_id, importer, _ = await make_tenant("agency-import-held")
    colleague = await make_user(admin_session, tenant_id, "held@agency.test")
    await make_candidate(
        admin_session, tenant_id, owner_id=colleague, full_name="Held Person",
        email="held@x.com",
    )
    await admin_session.commit()

    outcome = await run_import(
        tenant_id,
        importer,
        [{"full_name": "Held Person", "email": "held@x.com", "current_title": "CTO"}],
    )
    assert outcome.held_by_colleagues == 1
    assert outcome.candidates_updated == 0

    unchanged = (
        await admin_session.execute(
            text("SELECT current_title FROM candidates WHERE email = 'held@x.com'")
        )
    ).one()
    assert unchanged.current_title is None, "an import edited a colleague's candidate"


@pytest.mark.asyncio
async def test_an_import_skips_roles_for_a_row_a_colleague_holds(
    admin_session, seeded, run_import
) -> None:
    """A skipped candidate is skipped for its employment history too — writing
    a `CandidateRole` onto a held candidate is the same bulk edit the
    ownership guard exists to prevent, just arriving by a different path."""
    make_tenant, _, _ = seeded
    tenant_id, importer, _ = await make_tenant("agency-import-held-roles")
    colleague = await make_user(admin_session, tenant_id, "held2@agency.test")
    await make_candidate(
        admin_session, tenant_id, owner_id=colleague, full_name="Held Person",
        email="held2@x.com",
    )
    await admin_session.commit()

    outcome = await run_import(
        tenant_id,
        importer,
        [{"full_name": "Held Person", "email": "held2@x.com", "current_title": "CTO"}],
        role_rows=[
            {
                "candidate_email": "held2@x.com",
                "employer": "Acme Corp",
                "title": "Engineer",
            }
        ],
    )
    assert outcome.held_by_colleagues == 1
    assert outcome.candidates_updated == 0
    assert outcome.roles_created == 0

    unchanged = (
        await admin_session.execute(
            text("SELECT current_title FROM candidates WHERE email = 'held2@x.com'")
        )
    ).one()
    assert unchanged.current_title is None, "an import edited a colleague's candidate"

    role_rows = (
        await admin_session.execute(
            text(
                "SELECT cr.id FROM candidate_roles cr "
                "JOIN candidates c ON c.id = cr.candidate_id "
                "WHERE c.email = 'held2@x.com'"
            )
        )
    ).all()
    assert role_rows == [], "an import wrote a role onto a colleague's candidate"


@pytest.mark.asyncio
async def test_an_import_with_null_uploader_lands_everything_unowned(
    admin_session, seeded, run_import
) -> None:
    """`CandidateImport.uploaded_by` is nullable. A NULL uploader collapses
    the ownership guard to "skip every already-owned row" — conservative, no
    permission is gained — but a created row lands unowned in the shared
    queue rather than skipped. Pinned here so that is a decision, not an
    accident."""
    make_tenant, _, _ = seeded
    tenant_id, _, _ = await make_tenant("agency-import-null-uploader")
    await admin_session.commit()

    outcome = await run_import(
        tenant_id, None, [{"full_name": "New Person", "email": "null-owner@x.com"}]
    )
    assert outcome.candidates_created == 1
    assert outcome.held_by_colleagues == 0

    row = (
        await admin_session.execute(
            text("SELECT owner_id FROM candidates WHERE email = 'null-owner@x.com'")
        )
    ).one()
    assert row.owner_id is None
