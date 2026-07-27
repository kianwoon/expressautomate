"""Schema-level guarantees for the ingestion tables (plan §8, §10, §18, §19).

`admin_session` is used where a constraint must be observed directly: RLS would
otherwise hide a violation behind an empty result set, and a test that cannot
tell "rejected" from "invisible" proves nothing.

allow-hardcode: the INSERT statements below are test fixture data, not matching
logic — there is no list here that production behaviour is keyed on.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.db.session import SessionLocal


async def _add_mailbox(
    session, tenant_id, *, ms_user_id="ms-user", scope="whole_inbox", user_id=None
):
    mailbox_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, user_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, :owner, :user, 'inbox-folder', :scope, 24)"
        ),
        {
            "id": mailbox_id,
            "tenant": tenant_id,
            "owner": user_id,
            "user": ms_user_id,
            "scope": scope,
        },
    )
    return mailbox_id


async def _add_email(
    session,
    tenant_id,
    mailbox_id,
    graph_id,
    *,
    internet_id=None,
    with_statuses=True,
    on_conflict_ignore=False,
):
    """Insert one email row. Returns its id, or None if a replay was ignored."""
    columns = ["id", "tenant_id", "mailbox_id", "graph_message_id", "internet_message_id"]
    values = [":id", ":tenant", ":mailbox", ":graph", ":internet"]
    if with_statuses:
        columns += ["processing_status", "source_state", "classification_status"]
        values += ["'pending'", "'present'", "'unknown'"]

    conflict = (
        " ON CONFLICT (tenant_id, mailbox_id, graph_message_id) DO NOTHING"
        if on_conflict_ignore
        else ""
    )
    row_id = uuid.uuid4()
    result = await session.execute(
        text(
            f"INSERT INTO email_messages ({', '.join(columns)})"
            f" VALUES ({', '.join(values)}){conflict} RETURNING id"
        ),
        {
            "id": row_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "graph": graph_id,
            "internet": internet_id,
        },
    )
    return result.scalar_one_or_none()


async def _add_subscription(
    session, tenant_id, mailbox_id, subscription_id, *, state="secret", status="active"
):
    row_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at, status)"
            " VALUES (:id, :tenant, :mailbox, :sub, '/me/mailFolders/x/messages', :state,"
            "         now() + interval '1 day', :status)"
        ),
        {
            "id": row_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "sub": subscription_id,
            "state": state,
            "status": status,
        },
    )
    return row_id


@pytest.fixture
async def tenant(admin_session):
    tenant_id = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": tenant_id, "name": "Test Agency", "slug": f"test-{tenant_id.hex[:8]}"},
    )
    await admin_session.commit()
    yield tenant_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


@pytest.fixture
async def mailbox(admin_session, tenant):
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await admin_session.commit()
    return mailbox_id


async def test_duplicate_graph_message_id_in_same_mailbox_is_rejected(
    admin_session, tenant, mailbox
):
    await _add_email(admin_session, tenant, mailbox, "AAA")

    # Raised by the INSERT itself, not deferred to commit: the constraint is
    # immediate, and the pipeline relies on that — `record_notification` uses
    # ON CONFLICT DO NOTHING to make a replayed notification a no-op.
    with pytest.raises(IntegrityError):
        await _add_email(admin_session, tenant, mailbox, "AAA")
    await admin_session.rollback()


async def test_a_replayed_notification_is_a_no_op(admin_session, tenant, mailbox):
    """Every recovery path — webhook retry, delta sync, backfill — may arrive at
    the same message. Replay has to be free, or the recovery layer becomes a
    source of duplicates rather than a cure for gaps.

    allow-hardcode: one INSERT statement reused twice, not a phrase list.
    """
    ids = [
        await _add_email(admin_session, tenant, mailbox, "REPLAY", on_conflict_ignore=True)
        for _ in range(2)
    ]
    await admin_session.commit()

    assert ids[0] is not None
    assert ids[1] is None, "the replay must not insert a second row"

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM email_messages WHERE graph_message_id = :g"),
            {"g": "REPLAY"},
        )
    ).scalar_one()
    assert count == 1


async def test_status_columns_default_without_being_named(admin_session, tenant, mailbox):
    """The pipeline inserts with raw SQL, where a Python-side default never
    fires — these defaults have to live in the database or the NOT NULL
    constraints fail at runtime.
    """
    await _add_email(admin_session, tenant, mailbox, "DEFAULTS", with_statuses=False)
    await admin_session.commit()

    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, source_state, classification_status,"
                " attempt_count FROM email_messages WHERE graph_message_id = :g"
            ),
            {"g": "DEFAULTS"},
        )
    ).one()

    assert row.processing_status == "pending"
    assert row.source_state == "present"
    assert row.classification_status == "unknown"
    assert row.attempt_count == 0


async def test_same_internet_message_id_survives_in_a_second_mailbox(admin_session, tenant):
    """Two recruiters CC'd on one email must each keep their own row.

    A tenant-wide constraint would silently discard the second copy, and with it
    the fact that the second recruiter received it at all.
    """
    shared = "<shared@example.com>"
    for n in range(2):
        box = await _add_mailbox(admin_session, tenant, ms_user_id=f"ms-user-{n}")
        await _add_email(admin_session, tenant, box, f"AAA-{n}", internet_id=shared)
    await admin_session.commit()

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM email_messages WHERE internet_message_id = :m"),
            {"m": shared},
        )
    ).scalar_one()
    assert count == 2


async def test_resolve_subscription_works_without_tenant_context(
    admin_session, tenant, mailbox
):
    """The webhook is unauthenticated and has no tenant context.

    This function is the only pre-tenant read path in the system, which is why
    it returns three routing columns and nothing else.
    """
    await _add_subscription(admin_session, tenant, mailbox, "sub-1", state="secret-1")
    await admin_session.commit()

    async with SessionLocal() as session:  # runtime role, app.tenant_id never set
        row = (
            await session.execute(
                text("SELECT * FROM resolve_subscription(:s)"), {"s": "sub-1"}
            )
        ).one()

    assert row.tenant_id == tenant
    assert row.mailbox_id == mailbox
    assert row.client_state == "secret-1"


async def test_resolve_subscription_ignores_a_retired_subscription(
    admin_session, tenant, mailbox
):
    """A replaced subscription must stop routing, or notifications for a dead
    subscription keep resolving to a live mailbox."""
    await _add_subscription(admin_session, tenant, mailbox, "sub-old", status="replaced")
    await admin_session.commit()

    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT * FROM resolve_subscription(:s)"), {"s": "sub-old"}
            )
        ).one_or_none()

    assert row is None


async def test_ingestion_tables_are_tenant_isolated(admin_session, tenant, mailbox):
    """The owning tenant sees its rows; another tenant sees none.

    Both halves matter. A policy of `USING (false)` would pass the isolation
    half while making the product useless, so the positive read is what proves
    the policy discriminates rather than just denies.
    """
    await _add_email(admin_session, tenant, mailbox, "BBB")
    await _add_subscription(admin_session, tenant, mailbox, "sub-iso")
    await admin_session.commit()

    tables = ("mailboxes", "email_messages", "graph_subscriptions")

    async with tenant_session(tenant) as owner:
        mine = {
            t: (await owner.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()
            for t in tables
        }

    async with tenant_session(uuid.uuid4()) as other:
        theirs = {
            t: (await other.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()
            for t in tables
        }

    assert mine == {"mailboxes": 1, "email_messages": 1, "graph_subscriptions": 1}
    assert theirs == {"mailboxes": 0, "email_messages": 0, "graph_subscriptions": 0}


async def test_a_tenant_cannot_write_a_row_belonging_to_another_tenant(
    admin_session, tenant, mailbox
):
    """WITH CHECK, not just USING: a policy with only USING would let a tenant
    insert rows it could never read back."""
    async with tenant_session(uuid.uuid4()) as intruder:
        with pytest.raises(Exception) as excinfo:
            await intruder.execute(
                text(
                    "INSERT INTO email_messages"
                    " (id, tenant_id, mailbox_id, graph_message_id)"
                    " VALUES (:id, :tenant, :mailbox, 'INTRUDER')"
                ),
                {"id": uuid.uuid4(), "tenant": tenant, "mailbox": mailbox},
            )
    assert "row-level security" in str(excinfo.value).lower()


async def test_updated_at_advances_on_a_raw_sql_update(admin_session, tenant, mailbox):
    """`rescan_stuck` ages rows by `updated_at`, and every pipeline write is raw
    SQL — where SQLAlchemy's `onupdate` never fires. Without a database trigger
    a row that had just advanced would still look stalled, be requeued, and go
    round again until it ran out of attempts.
    """
    await _add_email(admin_session, tenant, mailbox, "TOUCH")
    await admin_session.commit()

    before = (
        await admin_session.execute(
            text("SELECT updated_at FROM email_messages WHERE graph_message_id = :g"),
            {"g": "TOUCH"},
        )
    ).scalar_one()

    await admin_session.execute(
        text(
            "UPDATE email_messages SET processing_status = 'fetched'"
            " WHERE graph_message_id = :g"
        ),
        {"g": "TOUCH"},
    )
    await admin_session.commit()

    after = (
        await admin_session.execute(
            text("SELECT updated_at FROM email_messages WHERE graph_message_id = :g"),
            {"g": "TOUCH"},
        )
    ).scalar_one()

    assert after > before


async def test_the_same_folder_cannot_be_connected_twice(admin_session, tenant):
    """Both email dedup constraints are scoped by mailbox_id, so a duplicate
    mailbox row would make every message ingest twice."""
    await _add_mailbox(admin_session, tenant)

    with pytest.raises(IntegrityError):
        await _add_mailbox(admin_session, tenant)
    await admin_session.rollback()


async def test_deleting_a_user_keeps_their_mailbox_and_mail(admin_session, tenant):
    """A departing recruiter must not take the agency's job orders with them.
    Tenant deletion is the only case that removes rows (spec: Retention).
    """
    user_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, display_name, role)"
            " VALUES (:id, :tenant, :email, 'Departing Recruiter', 'member')"
        ),
        {"id": user_id, "tenant": tenant, "email": f"leaver-{user_id.hex[:8]}@example.com"},
    )
    box = await _add_mailbox(admin_session, tenant, user_id=user_id)
    await _add_email(admin_session, tenant, box, "SURVIVES")
    await admin_session.commit()

    await admin_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await admin_session.commit()

    row = (
        await admin_session.execute(
            text("SELECT user_id FROM mailboxes WHERE id = :id"), {"id": box}
        )
    ).one_or_none()
    assert row is not None, "the mailbox must outlive the user"
    assert row.user_id is None, "the link is cleared, not cascaded"

    mail = (
        await admin_session.execute(
            text("SELECT count(*) FROM email_messages WHERE mailbox_id = :m"), {"m": box}
        )
    ).scalar_one()
    assert mail == 1


async def test_deleting_a_tenant_cascades_to_every_ingestion_row(admin_session, tenant):
    """Tenant deletion is the one case that removes rows (spec: Retention)."""
    box = await _add_mailbox(admin_session, tenant)
    await _add_email(admin_session, tenant, box, "CCC")
    await _add_subscription(admin_session, tenant, box, "sub-cascade")
    await admin_session.commit()

    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant}
    )
    await admin_session.commit()

    for table in ("mailboxes", "email_messages", "graph_subscriptions"):
        remaining = (
            await admin_session.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant}
            )
        ).scalar_one()
        assert remaining == 0, f"{table} still has rows for a deleted tenant"
