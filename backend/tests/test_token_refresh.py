"""Mailbox token acquisition (plan §8, §30).

Entra rotates refresh tokens on use, so the read-refresh-write sequence has to
be serialized per user. Getting the *order* wrong is the subtle failure: lock
after reading and both racers still hold the same pre-rotation token, so the
loser presents one Entra has already replaced and its mailbox is flagged
`needs_reauth` for a grant that is perfectly healthy.

allow-hardcode: the SQL below is test fixture data.
"""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.crypto import decrypt, encrypt
from app.db.rls import tenant_session
from app.services import ms_auth
from app.services.ms_auth import (
    MailboxNotAuthorised,
    TokenRefreshTransientError,
    access_token_for_mailbox,
)


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
            " VALUES (:id, :tenant, :email, 'recruiter')"
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


async def test_a_transient_entra_error_is_transient_not_a_dead_grant(
    monkeypatch, admin_session, mailbox_with_grant
):
    """A 429/throttle must not read as a dead grant.

    Entra returns 429 with a JSON body when it is throttling; MSAL does not
    raise on 429 (only >=500), it hands the body back as an error dict. Before
    the transient/permanent split this answered `MailboxNotAuthorised` — the
    caller flipped the mailbox to `needs_reauth` and the user had to reconnect
    manually for a grant that was perfectly healthy. That is the failure mode
    behind "the mailbox disconnects by itself": the delta sweep refreshes
    every 10 minutes, so any transient Entra hiccup permanently flagged the
    mailbox until a human re-consented.
    """
    tenant_id, user_id, mailbox_id = mailbox_with_grant

    class _ThrottledMsal:
        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            # The exact body Entra returns when throttling the token endpoint.
            return {
                "error": "temporarily_unavailable",
                "error_description": "AADSTS900429: The service is experiencing "
                "a temporary issue. Please retry.",
            }

    monkeypatch.setattr(ms_auth, "client", lambda: _ThrottledMsal())

    with pytest.raises(TokenRefreshTransientError):
        await access_token_for_mailbox(tenant_id, mailbox_id)

    # The grant is untouched — Entra never saw a *successful* refresh, so no
    # rotation happened and the stored token is still perfectly valid.
    assert await _stored_token(tenant_id, user_id) == "refresh-v1"


async def test_a_slow_refresh_that_answers_in_the_grace_period_still_succeeds(
    monkeypatch, admin_session, mailbox_with_grant
):
    """A refresh slower than the primary timeout must not burn the token.

    Before the grace period, `asyncio.wait_for` abandoned the executor thread
    at GRAPH_TIMEOUT_SECONDS. The thread kept running, Entra answered and
    rotated the refresh token (old one redeemed), but the new token was never
    persisted — the next refresh answered `invalid_grant` and the mailbox was
    genuinely dead until a human reconnected, for a grant that was healthy.
    Now the primary timeout is a soft signal and the grace window collects the
    still-running refresh.
    """
    tenant_id, user_id, mailbox_id = mailbox_with_grant
    monkeypatch.setattr(settings, "GRAPH_TIMEOUT_SECONDS", 0.1)

    class _SlowMsal:
        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            time.sleep(0.3)  # outlive the primary timeout, fit in the grace
            return {
                "access_token": "access-slow",
                "refresh_token": "refresh-v2",
                "scope": "Mail.Read",
            }

    monkeypatch.setattr(ms_auth, "client", lambda: _SlowMsal())

    token = await access_token_for_mailbox(tenant_id, mailbox_id)

    assert token == "access-slow"
    assert await _stored_token(tenant_id, user_id) == "refresh-v2", (
        "the rotation from the grace-period refresh must be persisted"
    )


async def test_a_refresh_that_exceeds_both_windows_is_transient(
    monkeypatch, admin_session, mailbox_with_grant
):
    """A refresh hung beyond the grace window is transient, never permanent."""
    tenant_id, user_id, mailbox_id = mailbox_with_grant
    monkeypatch.setattr(settings, "GRAPH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(settings, "TOKEN_REFRESH_GRACE_SECONDS", 0.05)

    class _HungMsal:
        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            time.sleep(0.4)
            return {"access_token": "never-seen", "refresh_token": "refresh-v2"}

    monkeypatch.setattr(ms_auth, "client", lambda: _HungMsal())

    with pytest.raises(TokenRefreshTransientError, match="timed out"):
        await access_token_for_mailbox(tenant_id, mailbox_id)

    # Not a `MailboxNotAuthorised`, so no caller flips the mailbox; the grant
    # row is exactly as it was.
    assert await _stored_token(tenant_id, user_id) == "refresh-v1"


async def test_a_genuinely_revoked_grant_is_still_permanent(
    monkeypatch, admin_session, mailbox_with_grant
):
    """The known permanent `invalid_grant` codes still force a reconnect."""
    tenant_id, user_id, mailbox_id = mailbox_with_grant

    class _RevokedMsal:
        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            return {
                "error": "invalid_grant",
                "error_description": (
                    "AADSTS700082: The refresh token has expired. Tokens are "
                    "valid for 90 days and then must be renewed."
                ),
            }

    monkeypatch.setattr(ms_auth, "client", lambda: _RevokedMsal())

    with pytest.raises(MailboxNotAuthorised, match="AADSTS700082"):
        await access_token_for_mailbox(tenant_id, mailbox_id)


async def test_an_unknown_invalid_grant_code_defaults_to_transient(
    monkeypatch, admin_session, mailbox_with_grant
):
    """An unrecognised failure leans transient: retry, not forced reconnect.

    The default matters because the cost of a wrong answer is asymmetric —
    wrongly retrying a dead grant costs noise in the sync events, while
    wrongly forcing a reconnect costs the user a full consent round trip.
    """
    tenant_id, user_id, mailbox_id = mailbox_with_grant

    class _MysteryMsal:
        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            return {
                "error": "invalid_grant",
                "error_description": "AADSTS99999: Some future failure we "
                "do not yet classify.",
            }

    monkeypatch.setattr(ms_auth, "client", lambda: _MysteryMsal())

    with pytest.raises(TokenRefreshTransientError):
        await access_token_for_mailbox(tenant_id, mailbox_id)

    assert await _stored_token(tenant_id, user_id) == "refresh-v1"


async def test_the_full_mailbox_scope_set_is_requested(
    monkeypatch, mailbox_with_grant
):
    """Mail permissions, and the identity ones alongside them.

    A token minted from identity-only consent 403s on every mail call. But
    asking for *only* the mail scope is equally wrong: incremental consent
    returns a token for exactly what was requested, so the rotated refresh
    token would come back narrower than the grant already stored.
    """
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
    assert any(s.lower() == "user.read" for s in seen["scopes"]), (
        "the identity scopes must ride along, or the rotated token narrows"
    )
