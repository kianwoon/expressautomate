"""Connecting a mailbox (plan §6.2).

Two steps, because consent is incremental. Signing in grants identity scopes
only — including for users who signed in before mailbox scopes existed — so
`/connect` starts a second consent and `/connect/callback` does the work once
it is granted.

Both the subscription and the backfill are queued rather than done inline, so a
large mailbox cannot hold the browser on the redirect. They may run in either
order: the dedup indexes make overlap free, and the delta sweep covers any gap
between them.

allow-hardcode: the SQL and payloads below are test fixtures.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.api import mailboxes as module
from app.api.mailboxes import ConnectRequest, resolve_start_date
from app.core.config import settings


@pytest.fixture(autouse=True)
def _microsoft_configured(monkeypatch):
    """Set explicitly rather than read from the environment.

    These tests assert what the handler does, not how the deployment is
    configured — and CI has no Microsoft credentials, so reading ambient config
    would make them fail there for a reason unrelated to the code under test.
    `tests/test_scopes.py` is where the deployed values are checked.
    """
    monkeypatch.setattr(settings, "MS_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "MS_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        settings,
        "MS_MAILBOX_REDIRECT_URI",
        "https://testserver/api/mailboxes/connect/callback",
    )
    monkeypatch.setattr(settings, "MS_MAILBOX_SCOPES", "Mail.Read")


@pytest.fixture
async def signed_in_user(admin_session):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role, ms_object_id)"
            " VALUES (:id, :tenant, :email, 'member', :oid)"
        ),
        {
            "id": user_id,
            "tenant": tenant_id,
            "email": f"u-{user_id.hex[:8]}@example.com",
            "oid": f"oid-{user_id.hex[:8]}",
        },
    )
    await admin_session.commit()

    # SimpleNamespace, not a class body: `tenant_id = tenant_id` there binds a
    # class-local and then reads it before assignment.
    yield SimpleNamespace(
        id=user_id, tenant_id=tenant_id, ms_object_id=f"oid-{user_id.hex[:8]}"
    )
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


# --- the historical bound ---------------------------------------------------


def test_a_start_date_beyond_the_lookback_is_clamped():
    """Graph delta filtered by receivedDateTime is not a bulk export mechanism.

    The UI must not offer what the implementation cannot deliver, and a silent
    clamp is better than a walk that stops somewhere unexplained.
    """
    requested = datetime.now(UTC) - timedelta(days=365)

    resolved = resolve_start_date(requested)

    earliest = datetime.now(UTC) - timedelta(days=settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS)
    assert resolved >= earliest - timedelta(seconds=5)


def test_a_recent_start_date_is_honoured():
    requested = datetime.now(UTC) - timedelta(days=3)

    assert resolve_start_date(requested) == requested


def test_a_future_start_date_is_pulled_back_to_now():
    """Otherwise the backfill filters for mail that cannot exist yet and the
    user sees an empty import with no explanation."""
    resolved = resolve_start_date(datetime.now(UTC) + timedelta(days=7))

    assert resolved <= datetime.now(UTC) + timedelta(seconds=5)


# --- the request shape ------------------------------------------------------


def test_whole_inbox_needs_no_folder():
    request = ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC))

    assert request.folder_id is None


def test_folder_scope_without_a_folder_is_rejected():
    with pytest.raises(ValueError):
        ConnectRequest(scope="folder", folder_id=None, start_from=datetime.now(UTC))


def test_an_unknown_scope_is_rejected():
    with pytest.raises(ValueError):
        ConnectRequest(scope="everything", start_from=datetime.now(UTC))


# --- step one: consent ------------------------------------------------------


async def test_connect_asks_only_for_mailbox_scopes(monkeypatch, signed_in_user):
    """A user who only signed in was never asked for mail. This is where that
    ask happens, and it should not re-request what is already granted."""
    captured = {}

    class _Msal:
        def initiate_auth_code_flow(self, scopes, redirect_uri):
            captured["scopes"] = scopes
            captured["redirect_uri"] = redirect_uri
            return {"auth_uri": "https://login.microsoftonline.com/authorize", "state": "s"}

    monkeypatch.setattr(module, "client", lambda: _Msal())

    response = await module.connect(
        ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC)),
        request=_FakeRequest(),
        user=signed_in_user,
    )

    assert any(s.lower() == "mail.read" for s in captured["scopes"])
    assert not any(s.lower() == "user.read" for s in captured["scopes"])
    assert captured["redirect_uri"] == settings.MS_MAILBOX_REDIRECT_URI
    assert response.status_code == 200


async def test_connect_creates_nothing_before_consent(
    monkeypatch, admin_session, signed_in_user
):
    """An abandoned consent screen must not leave a mailbox row behind for the
    sweeps to find and try to subscribe for, every fifteen minutes."""

    class _Msal:
        def initiate_auth_code_flow(self, scopes, redirect_uri):
            return {"auth_uri": "https://login.microsoftonline.com/x", "state": "s"}

    monkeypatch.setattr(module, "client", lambda: _Msal())

    await module.connect(
        ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC)),
        request=_FakeRequest(),
        user=signed_in_user,
    )

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM mailboxes WHERE tenant_id = :t"),
            {"t": signed_in_user.tenant_id},
        )
    ).scalar_one()
    assert count == 0


async def test_connect_hands_back_a_flow_cookie(monkeypatch, signed_in_user):
    """Returning the sealed flow in the body only would leave the callback with
    nothing to read, and every consent would fail on return."""

    class _Msal:
        def initiate_auth_code_flow(self, scopes, redirect_uri):
            return {"auth_uri": "https://login.microsoftonline.com/x", "state": "st8"}

    monkeypatch.setattr(module, "client", lambda: _Msal())

    response = await module.connect(
        ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC)),
        request=_FakeRequest(),
        user=signed_in_user,
    )

    assert "set-cookie" in response.headers


# --- step two: the callback -------------------------------------------------


async def test_the_callback_without_a_flow_is_a_client_error(signed_in_user):
    """Expired, evicted, or a callback that never had a flow. A bare dict
    lookup would make that a 500."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await module.connect_callback(
            request=_FakeRequest(query={"state": "unknown"}), user=signed_in_user
        )

    assert excinfo.value.status_code == 400


async def test_a_grant_for_a_different_microsoft_account_is_refused(
    monkeypatch, admin_session, signed_in_user
):
    """Entra shows an account picker, so the account that consents need not be
    the one that signed in.

    Unchecked, user A could consent as account B: B's refresh token would be
    stored under A, while the mailbox row claims A's `ms_user_id`. Every
    subsequent Graph call would then run B's token against A's mailbox path —
    cross-account confusion written into the database.
    """
    from fastapi import HTTPException

    state = "st8"
    sealed = module._seal_flow(
        {
            "state": state,
            "onboarding": ConnectRequest(
                scope="whole_inbox", start_from=datetime.now(UTC)
            ).model_dump(mode="json"),
        }
    )

    class _Msal:
        def acquire_token_by_auth_code_flow(self, flow, params):
            return {
                "refresh_token": "someone-elses",
                # A different Microsoft account than the one signed in.
                "id_token_claims": {"oid": "oid-somebody-else", "tid": "t"},
            }

    monkeypatch.setattr(module, "client", lambda: _Msal())

    with pytest.raises(HTTPException) as excinfo:
        await module.connect_callback(
            request=_FakeRequest(
                query={"state": state},
                cookies={module._flow_cookie_name(state): sealed},
            ),
            user=signed_in_user,
        )

    assert excinfo.value.status_code == 403
    mailboxes_written = (
        await admin_session.execute(
            text("SELECT count(*) FROM mailboxes WHERE tenant_id = :t"),
            {"t": signed_in_user.tenant_id},
        )
    ).scalar_one()
    # The stored foreign token is the actual attack — assert it explicitly
    # rather than relying on the check happening to sit before the write.
    tokens_written = (
        await admin_session.execute(
            text("SELECT count(*) FROM ms_oauth_tokens WHERE user_id = :u"),
            {"u": signed_in_user.id},
        )
    ).scalar_one()
    assert (mailboxes_written, tokens_written) == (0, 0)


async def test_a_sign_in_flow_cookie_cannot_drive_the_mailbox_callback(
    monkeypatch, signed_in_user
):
    """Same state, different flow. Reading the onboarding key directly would
    500 on something the user can simply retry."""
    from fastapi import HTTPException

    state = "shared"
    # A sign-in flow: no `onboarding` payload.
    sealed = module._seal_flow({"state": state})

    class _Msal:
        def acquire_token_by_auth_code_flow(self, flow, params):
            return {
                "refresh_token": "granted",
                "id_token_claims": {"oid": signed_in_user.ms_object_id, "tid": "t"},
            }

    monkeypatch.setattr(module, "client", lambda: _Msal())

    with pytest.raises(HTTPException) as excinfo:
        await module.connect_callback(
            request=_FakeRequest(
                query={"state": state},
                cookies={module._flow_cookie_name(state): sealed},
            ),
            user=signed_in_user,
        )

    assert excinfo.value.status_code == 400


async def test_the_matching_account_completes_onboarding(
    monkeypatch, admin_session, signed_in_user
):
    """The success path: token stored, mailbox created, both jobs queued."""
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(module, "enqueue", _enqueue)

    state = "st9"
    sealed = module._seal_flow(
        {
            "state": state,
            "onboarding": ConnectRequest(
                scope="folder",
                folder_id="jobs-folder",
                folder_name="Job Orders",
                start_from=datetime.now(UTC) - timedelta(days=3),
            ).model_dump(mode="json"),
        }
    )

    class _Msal:
        def acquire_token_by_auth_code_flow(self, flow, params):
            return {
                "refresh_token": "granted",
                "scope": "Mail.Read",
                "id_token_claims": {"oid": signed_in_user.ms_object_id, "tid": "t"},
            }

    monkeypatch.setattr(module, "client", lambda: _Msal())

    response = await module.connect_callback(
        request=_FakeRequest(
            query={"state": state}, cookies={module._flow_cookie_name(state): sealed}
        ),
        user=signed_in_user,
    )

    assert response.status_code == 303, "the user arrives here in a browser"

    row = (
        await admin_session.execute(
            text(
                "SELECT ms_user_id, scope, folder_id, folder_name, status,"
                " initial_sync_from, retention_months FROM mailboxes"
                " WHERE tenant_id = :t"
            ),
            {"t": signed_in_user.tenant_id},
        )
    ).one()
    assert row.ms_user_id == signed_in_user.ms_object_id
    assert row.folder_id == "jobs-folder"
    assert row.folder_name == "Job Orders"
    assert row.status == "active"
    assert row.retention_months == settings.DEFAULT_RETENTION_MONTHS

    stored = (
        await admin_session.execute(
            text(
                "SELECT refresh_token_encrypted FROM ms_oauth_tokens"
                " WHERE user_id = :u"
            ),
            {"u": signed_in_user.id},
        )
    ).scalar_one()
    from app.core.crypto import decrypt

    assert decrypt(stored) == "granted"

    assert [name for name, _ in queued] == [
        "recreate_subscription",
        "backfill_mailbox_job",
    ]


async def test_reconnecting_reuses_the_row_and_resubscribes(
    monkeypatch, admin_session, signed_in_user
):
    """The gap from the Task 9 review: a reconnected mailbox must not sit
    healthy-looking with no subscription until the hourly sweep notices."""
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(module, "enqueue", _enqueue)

    class _Msal:
        def acquire_token_by_auth_code_flow(self, flow, params):
            return {
                "refresh_token": "granted",
                "id_token_claims": {"oid": signed_in_user.ms_object_id, "tid": "t"},
            }

    monkeypatch.setattr(module, "client", lambda: _Msal())

    async def _connect(state: str):
        sealed = module._seal_flow(
            {
                "state": state,
                "onboarding": ConnectRequest(
                    scope="whole_inbox", start_from=datetime.now(UTC)
                ).model_dump(mode="json"),
            }
        )
        return await module.connect_callback(
            request=_FakeRequest(
                query={"state": state},
                cookies={module._flow_cookie_name(state): sealed},
            ),
            user=signed_in_user,
        )

    await _connect("first")
    await admin_session.execute(
        text("UPDATE mailboxes SET status = 'needs_reauth' WHERE tenant_id = :t"),
        {"t": signed_in_user.tenant_id},
    )
    await admin_session.commit()

    await _connect("second")

    rows = (
        await admin_session.execute(
            text("SELECT id, status FROM mailboxes WHERE tenant_id = :t"),
            {"t": signed_in_user.tenant_id},
        )
    ).all()
    assert len(rows) == 1, "reconnecting reuses the row rather than duplicating it"
    assert rows[0].status == "active"
    assert [name for name, _ in queued].count("recreate_subscription") == 2


async def test_a_google_only_user_cannot_connect_a_mailbox(signed_in_user):
    """There is no Gmail ingestion path (§6.1). Without an `ms_object_id` the
    row would carry a NULL `ms_user_id` — which never conflicts, so the upsert
    would duplicate on every attempt, and Graph URLs would contain `None`.
    """
    from fastapi import HTTPException

    signed_in_user.ms_object_id = None

    with pytest.raises(HTTPException) as excinfo:
        await module.connect(
            ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC)),
            request=_FakeRequest(),
            user=signed_in_user,
        )

    assert excinfo.value.status_code == 409


@pytest.mark.parametrize("path", ["/api/mailboxes/connect", "/api/mailboxes/connect/callback"])
async def test_both_routes_require_a_session(path):
    """Called through the ASGI app, so `Depends(current_user)` actually runs —
    invoking the handlers directly would never exercise it."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client_:
        response = await (
            client_.post(path, json={"scope": "whole_inbox", "start_from": "2026-07-27T00:00:00Z"})
            if path.endswith("connect")
            else client_.get(path)
        )

    assert response.status_code == 401


class _FakeRequest:
    """Enough of a Request for these handlers: cookies and query params."""

    def __init__(self, query: dict | None = None, cookies: dict | None = None) -> None:
        self.query_params = query or {}
        self.cookies = cookies or {}
