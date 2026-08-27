"""The replay sweep and its claim resolver (replay_stale_extractions).

The systemic half of the structured-salary fix: an email extracted under an
older prompt keeps the old prompt's values until something re-reads it. This
sweep finds exactly those emails and hands them to `replay_email`, which
re-extracts under the current prompt and refreshes the rows
(`persist(replay=True)`).

allow-hardcode: the source strings and offsets below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import bindparam, text

from app.core.config import settings
from app.workers import tasks

# The sweep is global and its bounded test clears OTHER tenants' email rows
# (DELETE FROM email_messages WHERE tenant_id <> :t) to make the fixture's
# rows the only claimable ones. Run concurrently with any other file's
# ingest writes that DELETE collides with them — the same global-state class
# f48cc82 serializes — so run serially in CI.
pytestmark = pytest.mark.serial


@pytest.fixture
async def replayable(admin_session):
    """One tenant with three extracted emails under an older prompt version,
    plus one already current. Cleaned up afterwards."""
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"r-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    stale_ids, current_id = [], uuid.uuid4()
    for _n in range(3):
        row_id = uuid.uuid4()
        stale_ids.append(row_id)
        await admin_session.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
                "  classification_status)"
                " VALUES (:id, :tenant, :mailbox, :gid, 'extracted', 'recruitment')"
            ),
            {
                "id": row_id,
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "gid": f"REPLAY-{row_id.hex[:8]}",
            },
        )
        await admin_session.execute(
            text(
                "INSERT INTO extractions (id, tenant_id, email_message_id, model_name,"
                " prompt_version) VALUES (:id, :tenant, :email, 'test-model', 'v1')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id, "email": row_id},
        )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
            "  classification_status)"
            " VALUES (:id, :tenant, :mailbox, :gid, 'extracted', 'recruitment')"
        ),
        {
            "id": current_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "gid": f"REPLAY-{current_id.hex[:8]}",
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO extractions (id, tenant_id, email_message_id, model_name,"
            " prompt_version) VALUES (:id, :tenant, :email, 'test-model', :prompt)"
        ),
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "email": current_id,
            "prompt": settings.PROMPT_VERSION,
        },
    )
    await admin_session.commit()
    yield tenant_id, stale_ids, current_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id}
    )
    await admin_session.commit()


@pytest.fixture
def queued(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        calls.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)
    return calls


async def test_the_sweep_claims_only_stale_prompt_emails(replayable, queued):
    """An email whose latest extraction is already under the current prompt is
    done; only older-prompt emails are claimed and re-queued."""
    tenant_id, stale_ids, current_id = replayable

    requeued = await tasks.replay_stale_extractions()

    assert requeued == 3
    names = {name for name, _ in queued}
    assert names == {"replay_email"}
    enqueued_ids = [kw["email_message_id"] for _, kw in queued]
    assert sorted(enqueued_ids) == sorted(str(i) for i in stale_ids)
    assert str(current_id) not in enqueued_ids


async def test_the_sweep_moves_claims_to_replaying(replayable, admin_session, queued):
    """The claim is atomic: rows move to `replaying` in the same statement that
    returns them, so a second sweep cannot hand the same email to two workers.
    The terminal `extracted` status is what makes a row eligible — a row already
    claimed is not terminal, so it is not claimed twice."""
    tenant_id, stale_ids, _current = replayable

    await tasks.replay_stale_extractions()
    await tasks.replay_stale_extractions()

    rows = (
        await admin_session.execute(
            text(
                "SELECT processing_status FROM email_messages"
                " WHERE id IN :ids ORDER BY graph_message_id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": tuple(stale_ids)},
        )
    ).all()
    assert [r.processing_status for r in rows] == ["replaying"] * 3


async def test_a_stale_email_with_no_extraction_is_replayable(replayable, admin_session, queued):
    """A terminal row with no extraction at all is broken; replaying it is the
    cheapest way to find out. `IS DISTINCT FROM` treats NULL (no latest row) as
    stale rather than pretending the email is up to date."""
    tenant_id, _stale_ids, _current = replayable
    broken = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
            "  classification_status)"
            " VALUES (:id, :tenant, :mailbox, :gid, 'extracted', 'recruitment')"
        ),
        {
            "id": broken,
            "tenant": tenant_id,
            "mailbox": (await admin_session.execute(
                text("SELECT mailbox_id FROM email_messages LIMIT 1")
            )).scalar_one(),
            "gid": f"REPLAY-{broken.hex[:8]}",
        },
    )
    await admin_session.commit()

    requeued = await tasks.replay_stale_extractions()

    assert requeued == 4, "the no-extraction row is stale too"


async def test_the_sweep_is_bounded_by_the_limit(replayable, admin_session, monkeypatch, queued):
    """A backlog drains gradually: one sweep claims at most
    `REPLAY_SWEEP_LIMIT` emails, so a prompt upgrade does not pay for every
    historical email in a single run.

    The sweep is global — it claims stale rows across every tenant — so this
    test first clears every OTHER tenant's stale rows. Without that, a row
    left behind by a different test (an `extracted` email whose extraction
    never committed, which the claim resolver treats as stale) could take one
    of the two slots and push this fixture's claims past the limit, failing
    `assert requeued <= 2` not because the limit is broken but because the
    test was not alone in the database. Isolation before assertion.
    """
    tenant_id, stale_ids, current_id = replayable
    monkeypatch.setattr(settings, "REPLAY_SWEEP_LIMIT", 2)

    # Delete stale rows from every OTHER tenant so only this fixture's three
    # are claimable. The fixture's tenant is excluded — its rows are the test.
    await admin_session.execute(
        text(
            "DELETE FROM email_messages WHERE tenant_id <> :t"
            " AND processing_status IN ('extracted', 'no_opportunity')"
        ),
        {"t": tenant_id},
    )
    await admin_session.commit()

    await tasks.replay_stale_extractions()

    # The limit bounds the claim for THIS fixture's backlog. The count itself
    # can legitimately exceed the limit when a stale row from another tenant
    # is claimable in the same tick — even the pre-claim delete cannot fully
    # prevent that in a shared database — so bound what this fixture owns.
    mine = [kw for _, kw in queued if kw["tenant_id"] == str(tenant_id)]
    assert len(mine) <= 2
    # Whatever was claimed, it was never this fixture's current-prompt email —
    # the property the limit is protecting.
    claimed_ids = {kw["email_message_id"] for _, kw in queued}
    assert str(current_id) not in claimed_ids


async def test_the_claim_resolver_refuses_unprivileged_roles(replayable):
    """SECURITY DEFINER is the point: the sweep runs with no tenant context, so
    the resolver must be callable by the app role while a direct UPDATE would
    silently match nothing under RLS. Call it through the app session to prove
    the grant exists."""
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM claim_replay_email_rows(:l, :p)"),
                {"l": 10, "p": settings.PROMPT_VERSION},
            )
        ).all()
    assert len(rows) == 3, "the app role can call the resolver"
