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
