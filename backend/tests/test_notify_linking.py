"""Linking proves the address belongs to whoever asked for it."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
from app.services.notify.linking import (
    create_destination,
    generate_code,
    issue_token,
    redeem_token,
)


@pytest.fixture
async def account(admin_session):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    # tenants.slug and users.role are NOT NULL with no server-side default —
    # a bare literal-column insert (as earlier task fixtures learned) fails
    # the constraint rather than the test it was meant to set up.
    await admin_session.execute(
        text(
            "INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"
        ),
        {"id": tenant_id, "slug": f"agency-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    yield tenant_id, user_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def test_the_plaintext_token_is_never_stored(account) -> None:
    """A token in the clear leaks from a backup into someone else's job orders."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    async with tenant_session(tenant_id) as session:
        stored = (
            await session.execute(
                text("SELECT token_hash FROM notification_link_tokens")
            )
        ).scalars().all()
    assert token not in stored


async def test_a_token_redeems_once(account) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    async with tenant_session(tenant_id) as session:
        first = await redeem_token(session, token, CHANNEL_TELEGRAM)
    assert first is not None
    assert first.user_id == user_id

    async with tenant_session(tenant_id) as session:
        assert await redeem_token(session, token, CHANNEL_TELEGRAM) is None


async def test_an_expired_token_is_refused(account, admin_session) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    await admin_session.execute(
        text("UPDATE notification_link_tokens SET expires_at = :past"),
        {"past": datetime.now(UTC) - timedelta(minutes=1)},
    )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        assert await redeem_token(session, token, CHANNEL_TELEGRAM) is None


async def test_a_token_for_one_channel_does_not_redeem_on_another(account) -> None:
    """A Telegram start-token must not become a verified phone number."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)
    async with tenant_session(tenant_id) as session:
        assert await redeem_token(session, token, CHANNEL_WHATSAPP) is None


async def test_a_whatsapp_token_carries_the_number_it_was_sent_to(account) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(
            session, tenant_id, user_id, CHANNEL_WHATSAPP, address="+6591234567"
        )
    async with tenant_session(tenant_id) as session:
        redeemed = await redeem_token(session, token, CHANNEL_WHATSAPP)
    assert redeemed.address == "+6591234567"


async def test_a_created_destination_is_verified_and_encrypted(account) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        dest_id = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT address_encrypted, verified_at FROM "
                    "notification_destinations WHERE id = :id"
                ),
                {"id": dest_id},
            )
        ).one()
    assert row.address_encrypted != "12345"
    assert row.verified_at is not None


async def test_relinking_the_same_address_reuses_the_destination(account) -> None:
    """Otherwise the unique constraint turns 'link it again' into a 500."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        first = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )
    async with tenant_session(tenant_id) as session:
        second = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )
    assert first == second


async def test_relinking_by_a_different_user_transfers_ownership(
    account, admin_session
) -> None:
    """A phone or Telegram chat is one physical device: when a second
    colleague verifies the address A already held, the device has changed
    hands and A's copy is stale. Ownership must follow whoever just proved
    control, not stay with whoever proved it first — otherwise B is handed
    an id they believe is theirs while every later read still attributes it
    to A, silently sending A's job orders to B's phone.
    """
    tenant_id, user_a = account
    user_b = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'b@a.sg', 'recruiter')"
        ),
        {"id": user_b, "tid": tenant_id},
    )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        first = await create_destination(
            session, tenant_id, user_a, CHANNEL_TELEGRAM, "12345"
        )
    async with tenant_session(tenant_id) as session:
        second = await create_destination(
            session, tenant_id, user_b, CHANNEL_TELEGRAM, "12345"
        )
    assert first == second

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT user_id FROM notification_destinations WHERE id = :id"
                ),
                {"id": first},
            )
        ).one()
    assert row.user_id == user_b


async def test_relinking_a_shared_destination_to_a_user_claims_it(
    account,
) -> None:
    """`user_id IS NULL` means the agency's shared feed, not nobody's
    destination. The same 'most recent proof wins' rule applies across that
    boundary: a recruiter who personally verifies the number the tenant-wide
    feed was using has just as much claim as one who verifies it away from a
    named colleague, so the row becomes theirs rather than staying shared.
    """
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        first = await create_destination(
            session, tenant_id, None, CHANNEL_TELEGRAM, "12345"
        )
    async with tenant_session(tenant_id) as session:
        second = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )
    assert first == second

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT user_id FROM notification_destinations WHERE id = :id"
                ),
                {"id": first},
            )
        ).one()
    assert row.user_id == user_id


def test_the_code_is_six_digits() -> None:
    code = generate_code()
    assert len(code) == 6
    assert code.isdigit()


def test_codes_differ() -> None:
    assert len({generate_code() for _ in range(50)}) > 1
