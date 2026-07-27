"""Mailbox token acquisition (plan §8, §30).

Entra rotates refresh tokens on use, so the read-refresh-write sequence has to
be serialized per user. Getting the *order* wrong is the subtle failure: lock
after reading and both racers still hold the same pre-rotation token, so the
loser presents one Entra has already replaced and its mailbox is flagged
`needs_reauth` for a grant that is perfectly healthy.

allow-hardcode: the SQL below is test fixture data.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.core.crypto import decrypt, encrypt
from app.db.rls import tenant_session
from app.services import ms_auth
from app.services.ms_auth import MailboxNotAuthorised, access_token_for_mailbox


@pytest.fixture
async def mailbox_with_grant(admin_session):
    tenant_id, user_id, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role)"
            " VALUES (:id, :tenant, :email, 'member')"
        ),
        {"id": user_id, "tenant": tenant_id, "email": f"u-{user_id.hex[:8]}@example.com"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO ms_oauth_tokens (id, tenant_id, user_id, refresh_token_encrypted)"
            " VALUES (:id, :tenant, :user, :token)"
        ),
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "user": user_id,
            "token": encrypt("refresh-v1"),
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, user_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, :user, 'ms-user', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id, "user": user_id},
    )
    await admin_session.commit()
    yield tenant_id, user_id, mailbox_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


class _FakeMsal:
    """Models Entra's rotation: each refresh token is valid exactly once."""

    def __init__(self) -> None:
        self.valid = {"refresh-v1"}
        self.issued = 0
        self.rejected = 0

    def acquire_token_by_refresh_token(self, refresh_token, scopes):
        if refresh_token not in self.valid:
            self.rejected += 1
            return {"error_description": "AADSTS70008: token already redeemed"}
        self.valid.discard(refresh_token)
        self.issued += 1
        rotated = f"refresh-v{self.issued + 1}"
        self.valid.add(rotated)
        return {
            "access_token": f"access-{self.issued}",
            "refresh_token": rotated,
            "scope": "Mail.Read",
        }


async def _stored_token(tenant_id, user_id) -> str:
    async with tenant_session(tenant_id) as session:
        raw = (
            await session.execute(
                text(
                    "SELECT refresh_token_encrypted FROM ms_oauth_tokens"
                    " WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
    return decrypt(raw)


async def test_a_token_is_acquired_and_the_rotation_is_persisted(
    monkeypatch, mailbox_with_grant
):
    tenant_id, user_id, mailbox_id = mailbox_with_grant
    fake = _FakeMsal()
    monkeypatch.setattr(ms_auth, "client", lambda: fake)

    token = await access_token_for_mailbox(tenant_id, mailbox_id)

    assert token == "access-1"
    assert await _stored_token(tenant_id, user_id) == "refresh-v2", (
        "the rotated token must be persisted, or the next refresh presents a "
        "token Entra has already redeemed"
    )


async def test_concurrent_refreshes_do_not_invalidate_each_other(
    monkeypatch, mailbox_with_grant
):
    """The lock must be held across read *and* refresh.

    Locking after the read would let both racers decrypt the same token; the
    second would then be rejected by Entra and its mailbox flagged
    `needs_reauth` despite a healthy grant.
    """
    tenant_id, user_id, mailbox_id = mailbox_with_grant
    fake = _FakeMsal()
    monkeypatch.setattr(ms_auth, "client", lambda: fake)

    tokens = await asyncio.gather(
        access_token_for_mailbox(tenant_id, mailbox_id),
        access_token_for_mailbox(tenant_id, mailbox_id),
    )

    assert fake.rejected == 0, "a racer presented an already-redeemed token"
    assert sorted(tokens) == ["access-1", "access-2"]
    assert await _stored_token(tenant_id, user_id) == "refresh-v3"


async def test_a_mailbox_without_an_owner_cannot_be_read(
    monkeypatch, admin_session, mailbox_with_grant
):
    """`user_id` is SET NULL when a recruiter is deleted — the mailbox and its
    mail survive, but nobody is left who authorised reading it."""
    tenant_id, _, mailbox_id = mailbox_with_grant
    await admin_session.execute(
        text("UPDATE mailboxes SET user_id = NULL WHERE id = :id"), {"id": mailbox_id}
    )
    await admin_session.commit()
    monkeypatch.setattr(ms_auth, "client", lambda: _FakeMsal())

    with pytest.raises(MailboxNotAuthorised):
        await access_token_for_mailbox(tenant_id, mailbox_id)


async def test_a_rejected_refresh_token_is_reported_as_unauthorised(
    monkeypatch, admin_session, mailbox_with_grant
):
    tenant_id, user_id, mailbox_id = mailbox_with_grant
    await admin_session.execute(
        text(
            "UPDATE ms_oauth_tokens SET refresh_token_encrypted = :token"
            " WHERE user_id = :user_id"
        ),
        {"token": encrypt("revoked"), "user_id": user_id},
    )
    await admin_session.commit()
    monkeypatch.setattr(ms_auth, "client", lambda: _FakeMsal())

    with pytest.raises(MailboxNotAuthorised):
        await access_token_for_mailbox(tenant_id, mailbox_id)


async def test_the_mailbox_scopes_are_requested_not_the_identity_ones(
    monkeypatch, mailbox_with_grant
):
    """A token minted from identity-only consent 403s on every mail call, and
    it does so at fetch time rather than here."""
    tenant_id, _, mailbox_id = mailbox_with_grant
    fake = _FakeMsal()
    seen = {}

    def _capture(refresh_token, scopes):
        seen["scopes"] = scopes
        return fake.acquire_token_by_refresh_token(refresh_token, scopes)

    fake_client = type("C", (), {"acquire_token_by_refresh_token": staticmethod(_capture)})()
    monkeypatch.setattr(ms_auth, "client", lambda: fake_client)

    await access_token_for_mailbox(tenant_id, mailbox_id)

    assert any(s.lower() == "mail.read" for s in seen["scopes"])
    assert not any(s.lower() == "user.read" for s in seen["scopes"])
