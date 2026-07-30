"""An event's recipient list must be intersected with subscriber destinations
correctly — including the tenant-level (`user_id IS NULL`) destination that
must keep receiving everything regardless of who is named."""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.dispatch import emit
from app.services.notify.events import EVENT_OPPORTUNITY_NEW, OpportunityEvent


@pytest.fixture
async def two_users_and_tenant_dest(admin_session):
    """One tenant, two users each with a verified destination subscribed to
    EVENT_OPPORTUNITY_NEW, plus one tenant-level destination (user_id NULL)
    also subscribed."""
    tenant_id = uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    dest_a, dest_b, dest_tenant = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id}"},
    )
    for uid, email in ((user_a, "a@agency.sg"), (user_b, "b@agency.sg")):
        await admin_session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :tid, :email, 'recruiter')"
            ),
            {"id": uid, "tid": tenant_id, "email": email},
        )

    for did, uid, phone in (
        (dest_a, user_a, "10001"),
        (dest_b, user_b, "10002"),
        (dest_tenant, None, "10003"),
    ):
        await admin_session.execute(
            text(
                "INSERT INTO notification_destinations "
                "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
                "VALUES (:id, :tid, :uid, :ch, 'ciphertext', :hash, now())"
            ),
            {
                "id": did,
                "tid": tenant_id,
                "uid": uid,
                "ch": CHANNEL_TELEGRAM,
                "hash": address_digest(phone),
            },
        )
        await admin_session.execute(
            text(
                "INSERT INTO notification_subscriptions "
                "(id, tenant_id, destination_id, event_kind, active) "
                "VALUES (:id, :tid, :did, :kind, true)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": did,
                "kind": EVENT_OPPORTUNITY_NEW,
            },
        )
    await admin_session.commit()
    yield tenant_id, user_a, user_b, dest_a, dest_b, dest_tenant
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _event(tenant_id: uuid.UUID, recipient_user_ids=None) -> OpportunityEvent:
    return OpportunityEvent(
        kind=EVENT_OPPORTUNITY_NEW,
        tenant_id=tenant_id,
        opportunity_id=uuid.uuid4(),
        job_title="Engineer",
        company_name="Acme",
        location="Singapore",
        salary="SGD 8,000",
        recipient_user_ids=recipient_user_ids,
    )


async def test_named_recipient_gets_it_but_the_other_user_does_not(
    two_users_and_tenant_dest,
) -> None:
    tenant_id, user_a, user_b, dest_a, dest_b, dest_tenant = two_users_and_tenant_dest
    async with tenant_session(tenant_id) as session:
        ids = await emit(_event(tenant_id, (user_b,)), session)

    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT destination_id FROM notification_deliveries WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            )
        ).all()
    destination_ids = {row.destination_id for row in rows}

    # Case 3: the tenant-level destination (user_id IS NULL) still receives
    # it even though the recipient list names a specific user.
    assert destination_ids == {dest_b, dest_tenant}
    assert dest_a not in destination_ids


async def test_none_recipients_preserves_tenant_wide_fanout(
    two_users_and_tenant_dest,
) -> None:
    tenant_id, user_a, user_b, dest_a, dest_b, dest_tenant = two_users_and_tenant_dest
    async with tenant_session(tenant_id) as session:
        ids = await emit(_event(tenant_id, None), session)

    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT destination_id FROM notification_deliveries WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            )
        ).all()
    destination_ids = {row.destination_id for row in rows}

    assert destination_ids == {dest_a, dest_b, dest_tenant}


async def test_tenant_level_destination_always_receives_named_recipients_event(
    two_users_and_tenant_dest,
) -> None:
    """Case 3 in isolation: even when recipients names neither user A nor B,
    the shared tenant-level destination must still be present."""
    tenant_id, user_a, user_b, dest_a, dest_b, dest_tenant = two_users_and_tenant_dest
    someone_else = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        ids = await emit(_event(tenant_id, (someone_else,)), session)

    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT destination_id FROM notification_deliveries WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            )
        ).all()
    destination_ids = {row.destination_id for row in rows}

    assert destination_ids == {dest_tenant}
