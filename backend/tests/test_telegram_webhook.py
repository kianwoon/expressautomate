"""The bot endpoint. Its URL is public, so the secret header is the whole gate.

ASGI transport, not TestClient: TestClient drives its own event loop and the
engine in app.db.session is pinned to the session-scoped one (see
test_notifications_api.py, which documents the same constraint).
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.linking import issue_token

HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture
async def client() -> httpx.AsyncClient:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
async def account(admin_session):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
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


def _update(token: str, chat_id: int = 555) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "text": f"/start {token}",
        },
    }


async def test_a_missing_secret_is_rejected(client) -> None:
    response = await client.post("/api/webhooks/telegram", json=_update("anything"))
    assert response.status_code == 401


async def test_a_wrong_secret_is_rejected(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    response = await client.post(
        "/api/webhooks/telegram",
        json=_update("anything"),
        headers={HEADER: "not-the-secret"},
    )
    assert response.status_code == 401


async def test_an_empty_configured_secret_rejects_everything(client, monkeypatch) -> None:
    """A missing/empty configured secret must REJECT, not fail open."""
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    response = await client.post(
        "/api/webhooks/telegram",
        json=_update("anything"),
        headers={HEADER: ""},
    )
    assert response.status_code == 401


async def test_a_valid_start_creates_a_verified_destination(
    client, account, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    response = await client.post(
        "/api/webhooks/telegram", json=_update(token), headers={HEADER: "s3cret"}
    )
    assert response.status_code == 200

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT channel, verified_at FROM notification_destinations "
                    "WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
        ).one()
    assert row.channel == CHANNEL_TELEGRAM
    assert row.verified_at is not None


async def test_an_unknown_token_creates_nothing(client, account, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    tenant_id, _ = account
    response = await client.post(
        "/api/webhooks/telegram",
        json=_update("not-a-real-token"),
        headers={HEADER: "s3cret"},
    )
    # 200 regardless: Telegram retries a non-2xx, and there is nothing to retry.
    assert response.status_code == 200
    async with tenant_session(tenant_id) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM notification_destinations")
            )
        ).scalar_one()
    assert count == 0


async def test_a_message_that_is_not_a_start_is_ignored(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    response = await client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 2,
            "message": {"message_id": 2, "chat": {"id": 1}, "text": "hello"},
        },
        headers={HEADER: "s3cret"},
    )
    assert response.status_code == 200


async def test_a_malformed_update_does_not_500(client, monkeypatch) -> None:
    """Telegram retries a 5xx, so a crash here becomes a retry loop."""
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    response = await client.post(
        "/api/webhooks/telegram", json={"update_id": 3}, headers={HEADER: "s3cret"}
    )
    assert response.status_code == 200


async def test_a_tenant_scoped_session_sees_only_its_own_link_tokens(
    admin_session, account
) -> None:
    """The genuine problem the brief tried to solve with a permissive RLS
    policy: an unscoped read has to resolve a token's tenant. This asserts
    the fix (a SECURITY DEFINER function) left the ordinary policy alone —
    a tenant-scoped session must still see only its own tokens, not another
    tenant's, and not the unscoped view the function itself uses.
    """
    tenant_id, user_id = account
    other_tenant_id = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'other', :slug)"),
        {"id": other_tenant_id, "slug": f"other-{other_tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'o@b.sg', 'recruiter')"
        ),
        {"id": uuid.uuid4(), "tid": other_tenant_id},
    )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    other_user_id = (
        await admin_session.execute(
            text("SELECT id FROM users WHERE tenant_id = :tid"),
            {"tid": other_tenant_id},
        )
    ).scalar_one()
    async with tenant_session(other_tenant_id) as session:
        await issue_token(session, other_tenant_id, other_user_id, CHANNEL_TELEGRAM)

    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(text("SELECT tenant_id FROM notification_link_tokens"))
        ).scalars().all()
    assert rows == [tenant_id]

    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant_id}
    )
    await admin_session.commit()


async def test_resolver_ignores_a_consumed_token(admin_session, account) -> None:
    """Matches redeem_token's own predicate: a spent token resolves to
    nothing, not to its former tenant."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)
        await session.execute(
            text(
                "UPDATE notification_link_tokens SET consumed_at = now() "
                "WHERE token_hash = :h"
            ),
            {"h": address_digest(token)},
        )

    resolved = (
        await admin_session.execute(
            text("SELECT resolve_link_token_tenant(:hash, :channel)"),
            {"hash": address_digest(token), "channel": CHANNEL_TELEGRAM},
        )
    ).scalar_one()
    assert resolved is None
