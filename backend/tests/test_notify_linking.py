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


def test_the_code_is_six_digits() -> None:
    code = generate_code()
    assert len(code) == 6
    assert code.isdigit()


def test_codes_differ() -> None:
    assert len({generate_code() for _ in range(50)}) > 1
