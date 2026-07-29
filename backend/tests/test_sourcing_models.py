"""Task 1: the three sourcing tables. Isolation, the submission uniqueness
guard, and the `sourcing_runs.state` whitelist — no scoring or routes yet.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.models.sourcing import CandidateSubmission, SourcingMatch, SourcingRun
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import _a_candidate_row, agency, other_agency  # noqa: F401


async def _a_client_row(tenant_id) -> uuid.UUID:
    client_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Acme Pte Ltd', 'acme pte ltd', 'confirmed')"
            ),
            {"i": client_id, "t": tenant_id},
        )
        await s.commit()
    return client_id


async def _a_run_row(tenant_id, opportunity_id) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        s.add(SourcingRun(id=run_id, tenant_id=tenant_id, opportunity_id=opportunity_id))
    return run_id


async def _an_opportunity_row(tenant_id) -> uuid.UUID:
    # `sourcing_runs.opportunity_id` is NOT NULL, so every fixture needing a
    # run needs a real opportunity row too, which in turn needs a mailbox and
    # an email row — the same chain `tests/test_extraction_schema.py` builds.
    mailbox_id = uuid.uuid4()
    message_id = uuid.uuid4()
    opportunity_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
                " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " processing_status, source_state, classification_status)"
                " VALUES (:i, :t, :m, :g, 'fetched', 'present', 'recruitment')"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id, "g": f"msg-{message_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " review_status, quality_state) VALUES (:i, :t, :e, 'ready', 'likely')"
            ),
            {"i": opportunity_id, "t": tenant_id, "e": message_id},
        )
        await s.commit()
    return opportunity_id


@pytest.fixture(autouse=True)
async def _cleanup_sourcing_tables():
    yield
    async with AdminSessionLocal() as s:
        for table in (
            "sourcing_matches",
            "sourcing_runs",
            "candidate_submissions",
            "opportunities",
            "email_messages",
            "mailboxes",
            "clients",
        ):
            await s.execute(text(f"DELETE FROM {table}"))
        await s.commit()


async def test_candidate_submission_isolated_by_tenant(agency, other_agency):  # noqa: F811
    tenant_id, user_id = agency
    other_tenant_id, _ = other_agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    client_id = await _a_client_row(tenant_id)

    async with tenant_session(tenant_id) as s:
        s.add(
            CandidateSubmission(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                client_id=client_id,
                submitted_by=user_id,
            )
        )

    async with tenant_session(tenant_id) as s:
        rows = (await s.execute(select(CandidateSubmission))).scalars().all()
    assert len(rows) == 1

    async with tenant_session(other_tenant_id) as s:
        rows = (await s.execute(select(CandidateSubmission))).scalars().all()
    assert rows == []


async def test_sourcing_run_isolated_by_tenant(agency, other_agency):  # noqa: F811
    tenant_id, _ = agency
    other_tenant_id, _ = other_agency
    opportunity_id = await _an_opportunity_row(tenant_id)
    run_id = await _a_run_row(tenant_id, opportunity_id)

    async with tenant_session(tenant_id) as s:
        rows = (await s.execute(select(SourcingRun))).scalars().all()
    assert [r.id for r in rows] == [run_id]

    async with tenant_session(other_tenant_id) as s:
        rows = (await s.execute(select(SourcingRun))).scalars().all()
    assert rows == []


async def test_sourcing_match_isolated_by_tenant(agency, other_agency):  # noqa: F811
    tenant_id, user_id = agency
    other_tenant_id, _ = other_agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    opportunity_id = await _an_opportunity_row(tenant_id)
    run_id = await _a_run_row(tenant_id, opportunity_id)

    async with tenant_session(tenant_id) as s:
        s.add(
            SourcingMatch(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                candidate_id=candidate_id,
                score=87.5,
                reasons=["5 years matching skill"],
            )
        )

    async with tenant_session(tenant_id) as s:
        rows = (await s.execute(select(SourcingMatch))).scalars().all()
    assert len(rows) == 1
    assert rows[0].score == pytest.approx(87.5)

    async with tenant_session(other_tenant_id) as s:
        rows = (await s.execute(select(SourcingMatch))).scalars().all()
    assert rows == []


async def test_second_submission_of_same_candidate_to_same_client_rejected(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    client_id = await _a_client_row(tenant_id)

    async with tenant_session(tenant_id) as s:
        s.add(
            CandidateSubmission(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                client_id=client_id,
            )
        )

    # The whole `async with tenant_session(...)` block must be inside
    # `pytest.raises`: the context manager commits again on `__aexit__`, and
    # in an already-aborted transaction that second commit raises
    # `PendingRollbackError` outside a `raises` that only wrapped the add.
    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_id) as s:
            s.add(
                CandidateSubmission(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    candidate_id=candidate_id,
                    client_id=client_id,
                )
            )


async def test_sourcing_run_state_rejects_value_outside_whitelist(agency):  # noqa: F811
    tenant_id, _ = agency
    opportunity_id = await _an_opportunity_row(tenant_id)

    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_id) as s:
            s.add(
                SourcingRun(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    state="bogus",
                )
            )
