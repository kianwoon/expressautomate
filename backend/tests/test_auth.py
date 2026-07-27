"""Microsoft sign-in end to end, with MSAL mocked out.

Nothing here touches the network: `ms_auth.begin_login` / `complete_login` are
the only two places the real MSAL client is used, so replacing them isolates
the whole flow. What is *not* mocked is the database — provisioning a tenant
happens before any tenant context exists, and RLS is the part most likely to
break silently, so those statements run for real.
"""

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.api import auth as auth_api
from app.core import crypto
from app.core.config import settings
from app.core.crypto import decrypt
from app.db.rls import tenant_session
from app.main import app
from app.services import ms_auth

AUTHORIZE_HOST = "https://login.microsoftonline.com"

# Derived, never a literal: FRONTEND_ORIGIN differs between CI and a dev .env.
DASHBOARD_URL = settings.FRONTEND_ORIGIN.rstrip("/") + auth_api.DASHBOARD_PATH


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """ASGI transport, not TestClient: TestClient drives its own event loop and
    the engine in app.db.session is pinned to the session-scoped one."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def microsoft_configured(monkeypatch) -> None:
    """Credentials the suite supplies itself.

    Without this the routes 503 wherever `MS_CLIENT_ID` / `MS_CLIENT_SECRET`
    are absent, which passes on a developer machine — the repo-root `.env`
    holds real credentials — and fails in CI, where it must not depend on them.

    Unconditional, not `settings.X or <default>`: falling back only when unset
    would leave the dev machine running the suite under different values from
    CI, which is the whole failure mode this exists to remove. Nothing here
    reaches MSAL — `begin_login` and `complete_login` are both patched — so the
    real credentials are never needed.
    """
    monkeypatch.setattr(settings, "MS_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(settings, "MS_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MS_TENANT_ID", "common")
    monkeypatch.setattr(
        settings, "MS_REDIRECT_URI", "https://testserver/api/auth/microsoft/callback"
    )
    # Set explicitly rather than relying on the environment: these tests assert
    # on what sign-in requests, and reading that from ambient config would make
    # them pass or fail for reasons unrelated to the code under test.
    # tests/test_scopes.py is where the deployed values are checked.
    #
    # Two keys, because consent is incremental: sign-in asks for identity,
    # connecting a mailbox asks for mail.
    monkeypatch.setattr(
        settings, "MS_IDENTITY_SCOPES", "openid profile email User.Read offline_access"
    )
    monkeypatch.setattr(settings, "MS_MAILBOX_SCOPES", "Mail.Read")


@pytest.fixture(autouse=True)
def fake_msal(monkeypatch) -> None:
    """A believable flow dict and token response, with no MSAL client built."""

    def begin_login() -> dict:
        state = uuid.uuid4().hex
        return {
            "state": state,
            "code_verifier": uuid.uuid4().hex,
            "nonce": uuid.uuid4().hex,
            "redirect_uri": settings.MS_REDIRECT_URI,
            "scope": ms_auth.delegated_scopes("identity"),
            "auth_uri": f"{AUTHORIZE_HOST}/organizations/oauth2/v2.0/authorize?state={state}",
        }

    monkeypatch.setattr(ms_auth, "begin_login", begin_login)


def token_response(tid: str, oid: str, email: str, name: str = "Rachel Tan") -> dict:
    return {
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "scope": " ".join(ms_auth.delegated_scopes("identity")),
        "id_token_claims": {"tid": tid, "oid": oid, "preferred_username": email, "name": name},
    }


def state_of(login: httpx.Response) -> str:
    """The `state` Microsoft will echo back, read off the authorize URL."""
    return parse_qs(urlparse(login.headers["location"]).query)["state"][0]


def flow_cookies(client: httpx.AsyncClient) -> list[str]:
    return [n for n in client.cookies.keys() if n.startswith(auth_api.FLOW_COOKIE_PREFIX)]


async def sign_in(client: httpx.AsyncClient, monkeypatch, result: dict) -> httpx.Response:
    login = await client.get("/api/auth/microsoft/login")
    assert login.status_code == 307
    monkeypatch.setattr(ms_auth, "complete_login", lambda flow, params: result)
    return await client.get(
        "/api/auth/microsoft/callback",
        params={"code": "any-code", "state": state_of(login)},
    )


@pytest.fixture
async def cleanup() -> list[uuid.UUID]:
    """Tenants to remove afterwards; RLS means each needs its own scope."""
    tenants: list[uuid.UUID] = []
    yield tenants
    for tid in tenants:
        async with tenant_session(tid) as s:
            await s.execute(text("DELETE FROM ms_oauth_tokens"))
            await s.execute(text("DELETE FROM users"))
            await s.execute(text("DELETE FROM tenants"))


async def test_login_redirects_to_the_microsoft_authorize_endpoint(client) -> None:
    response = await client.get("/api/auth/microsoft/login")
    assert response.status_code == 307
    assert response.headers["location"].startswith(f"{AUTHORIZE_HOST}/")
    assert "authorize" in response.headers["location"]
    assert auth_api._flow_cookie_name(state_of(response)) in response.cookies


async def test_login_does_not_leak_the_flow_secrets_into_the_cookie(client) -> None:
    """The cookie is encrypted, not merely signed — the PKCE verifier is secret.

    Asserting only that the verifier is absent as a substring would not catch
    the regression this guards: a signed-but-unencrypted itsdangerous payload
    is compressed and base64'd, so the plaintext would not appear literally
    there either. The real property is that the bytes are unreadable without
    the key, so that is what is checked.
    """
    response = await client.get("/api/auth/microsoft/login")
    sealed = response.cookies[auth_api._flow_cookie_name(state_of(response))]

    assert auth_api._open_flow(sealed)["code_verifier"] not in sealed
    # A different key must not open it — proves encryption, not just encoding.
    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(sealed.encode())


async def test_callback_creates_the_tenant_and_user(client, monkeypatch, cleanup) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    response = await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    assert response.status_code == 307
    assert response.headers["location"] == DASHBOARD_URL

    async with tenant_session(uuid.UUID(tid)) as s:
        rows = (
            await s.execute(text("SELECT email, ms_object_id, last_login_at FROM users"))
        ).all()
        ms_tenant_id = (await s.execute(text("SELECT ms_tenant_id FROM tenants"))).scalar_one()
    assert [(r[0], r[1]) for r in rows] == [("rachel@agency-a.sg", oid)]
    assert rows[0][2] is not None
    assert ms_tenant_id == tid


async def test_refresh_token_is_stored_encrypted(client, monkeypatch, cleanup) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    async with tenant_session(uuid.UUID(tid)) as s:
        stored = (
            await s.execute(text("SELECT refresh_token_encrypted FROM ms_oauth_tokens"))
        ).scalar_one()
    assert stored != "refresh-token-value", "refresh token stored in clear"
    assert decrypt(stored) == "refresh-token-value"


async def test_second_sign_in_is_idempotent(client, monkeypatch, cleanup) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    result = token_response(tid, oid, "rachel@agency-a.sg")
    await sign_in(client, monkeypatch, result)
    await sign_in(client, monkeypatch, result | {"refresh_token": "second-refresh-token"})

    async with tenant_session(uuid.UUID(tid)) as s:
        assert (await s.execute(text("SELECT count(*) FROM tenants"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
        stored = (
            await s.execute(text("SELECT refresh_token_encrypted FROM ms_oauth_tokens"))
        ).scalar_one()
    assert decrypt(stored) == "second-refresh-token", "refresh token was not rotated"


async def test_a_different_entra_tenant_gets_its_own_tenant(client, monkeypatch, cleanup) -> None:
    """Two agencies signing in must never land in the same tenant (§18)."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    cleanup.extend([uuid.UUID(a), uuid.UUID(b)])
    await sign_in(client, monkeypatch, token_response(a, uuid.uuid4().hex, "rachel@agency-a.sg"))
    await sign_in(client, monkeypatch, token_response(b, uuid.uuid4().hex, "sam@agency-b.sg"))

    async with tenant_session(uuid.UUID(a)) as s:
        assert (await s.execute(text("SELECT email FROM users"))).scalars().all() == [
            "rachel@agency-a.sg"
        ]
    async with tenant_session(uuid.UUID(b)) as s:
        assert (await s.execute(text("SELECT email FROM users"))).scalars().all() == [
            "sam@agency-b.sg"
        ]


MSA = auth_api.MSA_CONSUMER_TENANT_ID


def personal_tenant_id(oid: str) -> uuid.UUID:
    return uuid.uuid5(auth_api.PERSONAL_TENANT_NAMESPACE, oid)


async def test_two_personal_accounts_are_isolated_from_each_other(
    client, monkeypatch, cleanup
) -> None:
    """All consumer accounts share one `tid`; keying on it would merge them."""
    oid_a, oid_b = uuid.uuid4().hex, uuid.uuid4().hex
    tenant_a, tenant_b = personal_tenant_id(oid_a), personal_tenant_id(oid_b)
    cleanup.extend([tenant_a, tenant_b])

    await sign_in(client, monkeypatch, token_response(MSA, oid_a, "rachel@hotmail.com"))
    await sign_in(client, monkeypatch, token_response(MSA, oid_b, "sam@outlook.com"))

    assert tenant_a != tenant_b
    async with tenant_session(tenant_a) as s:
        assert (await s.execute(text("SELECT email FROM users"))).scalars().all() == [
            "rachel@hotmail.com"
        ]
        assert (await s.execute(text("SELECT count(*) FROM ms_oauth_tokens"))).scalar_one() == 1
    async with tenant_session(tenant_b) as s:
        assert (await s.execute(text("SELECT email FROM users"))).scalars().all() == [
            "sam@outlook.com"
        ]
        assert (await s.execute(text("SELECT count(*) FROM ms_oauth_tokens"))).scalar_one() == 1


async def test_a_personal_tenant_is_flagged_and_keeps_the_real_msa_tid(
    client, monkeypatch, cleanup
) -> None:
    oid = uuid.uuid4().hex
    tenant = personal_tenant_id(oid)
    cleanup.append(tenant)
    await sign_in(client, monkeypatch, token_response(MSA, oid, "rachel@live.com"))

    async with tenant_session(tenant) as s:
        flag, ms_tid, name = (
            await s.execute(text("SELECT is_personal_account, ms_tenant_id, name FROM tenants"))
        ).one()
    assert flag is True
    assert ms_tid == MSA, "the real MSA tid must still be recorded"
    # "live.com" would name every consumer tenant identically.
    assert name == "rachel"


async def test_a_personal_account_signing_in_twice_is_idempotent(
    client, monkeypatch, cleanup
) -> None:
    oid = uuid.uuid4().hex
    tenant = personal_tenant_id(oid)
    cleanup.append(tenant)
    result = token_response(MSA, oid, "rachel@hotmail.com")
    await sign_in(client, monkeypatch, result)
    await sign_in(client, monkeypatch, result)

    async with tenant_session(tenant) as s:
        assert (await s.execute(text("SELECT count(*) FROM tenants"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
        assert (await s.execute(text("SELECT count(*) FROM ms_oauth_tokens"))).scalar_one() == 1


async def test_colleagues_in_one_work_tenant_share_it(client, monkeypatch, cleanup) -> None:
    """The multi-recruiter agency case must be unaffected by the MSA handling."""
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "sam@agency-a.sg"))

    async with tenant_session(uuid.UUID(tid)) as s:
        emails = (await s.execute(text("SELECT email FROM users ORDER BY email"))).scalars().all()
        tenants = (await s.execute(text("SELECT count(*) FROM tenants"))).scalar_one()
        flag = (await s.execute(text("SELECT is_personal_account FROM tenants"))).scalar_one()
    assert emails == ["rachel@agency-a.sg", "sam@agency-a.sg"]
    assert tenants == 1
    assert flag is False


async def test_a_upn_rename_updates_the_existing_user(client, monkeypatch, cleanup) -> None:
    """Same Entra object id, new mail address — one user, not two."""
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel.tan@agency-a.sg"))

    async with tenant_session(uuid.UUID(tid)) as s:
        rows = (await s.execute(text("SELECT email FROM users"))).scalars().all()
        tokens = (await s.execute(text("SELECT count(*) FROM ms_oauth_tokens"))).scalar_one()
    assert rows == ["rachel.tan@agency-a.sg"], "the rename created a second user"
    assert tokens == 1, "the rename orphaned the stored refresh token"


async def test_a_replayed_callback_is_a_400(client, monkeypatch, cleanup) -> None:
    """MSAL raises ValueError on a state mismatch or a reused code — not a 500."""
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))

    def explode(flow: dict, params: dict) -> dict:
        raise ValueError("state mismatch")

    login = await client.get("/api/auth/microsoft/login")
    monkeypatch.setattr(ms_auth, "complete_login", explode)
    response = await client.get(
        "/api/auth/microsoft/callback",
        params={"code": "replayed", "state": state_of(login)},
    )
    assert response.status_code == 400


def test_a_placeholder_encryption_key_is_not_a_real_fernet_key() -> None:
    """In production such a key is refused; CI's placeholder still derives one."""
    # Literals, never settings: a developer .env holds a genuine key, so
    # asserting on the configured value passes in CI and fails on their machine.
    assert not crypto._is_real_fernet_key("changeme")
    assert not crypto._is_real_fernet_key("ci-not-a-real-key")
    assert crypto._is_real_fernet_key(Fernet.generate_key().decode())


def test_production_refuses_a_derived_encryption_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production")
    # Pin the key too — otherwise a developer .env supplies a real one and the
    # first half of this test, which asserts production refuses it, cannot fire.
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", "not-a-real-fernet-key")
    crypto._fernet.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="url-safe base64 Fernet key"):
            crypto.encrypt("secret")
        monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
        crypto._fernet.cache_clear()
        assert crypto.decrypt(crypto.encrypt("secret")) == "secret"
    finally:
        # The cache is process-wide; leaving a production Fernet in it would
        # make every later test decrypt under the wrong key.
        crypto._fernet.cache_clear()


async def test_me_is_401_without_a_cookie(client) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_returns_the_signed_in_user(client, monkeypatch, cleanup) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    body = (await client.get("/api/auth/me")).json()
    assert body["user"]["email"] == "rachel@agency-a.sg"
    assert body["user"]["display_name"] == "Rachel Tan"
    assert body["user"]["role"] == "recruiter"
    assert body["tenant"]["id"] == tid
    assert body["tenant"]["name"] == "agency-a.sg"
    # The dashboard is built against exactly these keys.
    assert set(body) == {"user", "tenant", "mailbox"}


async def test_me_reports_a_connected_mailbox_with_its_scopes(
    client, monkeypatch, cleanup
) -> None:
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["provider"] == "microsoft"
    assert mailbox["connected"] is True
    # Sign-in stores identity scopes only; mail access is consented separately
    # when a mailbox is connected.
    assert mailbox["scopes"] == ms_auth.delegated_scopes("identity")
    # No ingestion exists yet (§7); claiming otherwise would be a lie to the UI.
    assert mailbox["ingestion_active"] is False


async def test_me_reports_no_mailbox_when_no_token_was_stored(
    client, monkeypatch, cleanup
) -> None:
    """Microsoft can return no refresh token — onboarding must say so, not guess."""
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    result = token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg")
    del result["refresh_token"]
    await sign_in(client, monkeypatch, result)

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["connected"] is False
    assert mailbox["scopes"] == []


async def test_me_flags_a_personal_account_tenant(client, monkeypatch, cleanup) -> None:
    oid = uuid.uuid4().hex
    cleanup.append(personal_tenant_id(oid))
    await sign_in(client, monkeypatch, token_response(MSA, oid, "rachel@hotmail.com"))

    body = (await client.get("/api/auth/me")).json()
    assert body["tenant"]["is_personal_account"] is True
    assert body["tenant"]["id"] == str(personal_tenant_id(oid))


async def test_me_does_not_flag_a_work_tenant(client, monkeypatch, cleanup) -> None:
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    assert (await client.get("/api/auth/me")).json()["tenant"]["is_personal_account"] is False


async def test_me_is_401_when_the_session_user_no_longer_exists(
    client, monkeypatch, cleanup
) -> None:
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    async with tenant_session(uuid.UUID(tid)) as s:
        await s.execute(text("DELETE FROM ms_oauth_tokens"))
        await s.execute(text("DELETE FROM users"))
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_clears_the_session(client, monkeypatch, cleanup) -> None:
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    assert (await client.post("/api/auth/logout")).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_rejects_a_forged_cookie(client) -> None:
    client.cookies.set(auth_api.SESSION_COOKIE, "not-a-signed-value")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_callback_without_a_flow_cookie_is_rejected(client) -> None:
    response = await client.get(
        "/api/auth/microsoft/callback", params={"code": "x"}
    )
    assert response.status_code == 400


async def test_a_prerendered_second_login_does_not_break_the_first(
    client, monkeypatch, cleanup
) -> None:
    """The production bug: one click, two /login calls, callback on the first.

    Chrome prerenders the "Sign in" anchor, so the endpoint fires twice while
    the browser navigates with the *first* flow's state. With a single fixed
    cookie name the second response overwrote the first and the callback 400d.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))

    first = await client.get("/api/auth/microsoft/login")
    second = await client.get("/api/auth/microsoft/login")
    assert state_of(first) != state_of(second)
    assert len(flow_cookies(client)) == 2, "the second login clobbered the first"

    monkeypatch.setattr(
        ms_auth, "complete_login", lambda flow, params: token_response(tid, oid, "r@agency-a.sg")
    )
    response = await client.get(
        "/api/auth/microsoft/callback",
        params={"code": "any-code", "state": state_of(first)},
    )
    assert response.status_code == 307, response.text
    assert response.headers["location"] == DASHBOARD_URL


async def test_callback_lands_on_the_dashboard_not_the_marketing_page(
    client, monkeypatch, cleanup
) -> None:
    """A signed-in user must see the dashboard, on the frontend's own origin."""
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    response = await sign_in(
        client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg")
    )

    location = response.headers["location"]
    assert location != settings.FRONTEND_ORIGIN
    assert urlparse(location).path == auth_api.DASHBOARD_PATH
    origin = urlparse(settings.FRONTEND_ORIGIN)
    assert (urlparse(location).scheme, urlparse(location).netloc) == (origin.scheme, origin.netloc)


async def test_a_trailing_slash_on_the_origin_does_not_double_the_separator(
    client, monkeypatch, cleanup
) -> None:
    """`https://host/` + `/dashboard` must not become `https://host//dashboard`."""
    monkeypatch.setattr(settings, "FRONTEND_ORIGIN", "https://example.test/")
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    response = await sign_in(
        client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg")
    )

    assert response.headers["location"] == "https://example.test/dashboard"


async def test_the_callback_hands_msal_the_flow_matching_the_state(client, monkeypatch) -> None:
    """Not merely 'a' flow — the one whose state came back."""
    first = await client.get("/api/auth/microsoft/login")
    await client.get("/api/auth/microsoft/login")

    seen: dict = {}

    def capture(flow: dict, params: dict) -> dict:
        seen.update(flow)
        raise ValueError("stop here; the flow is all this test needs")

    monkeypatch.setattr(ms_auth, "complete_login", capture)
    await client.get(
        "/api/auth/microsoft/callback",
        params={"code": "any-code", "state": state_of(first)},
    )
    assert seen["state"] == state_of(first)


async def test_a_completed_callback_clears_every_in_flight_flow(
    client, monkeypatch, cleanup
) -> None:
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await client.get("/api/auth/microsoft/login")
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "r@agency-a.sg"))
    assert flow_cookies(client) == []


async def test_in_flight_flows_are_capped(client) -> None:
    """A repeatedly hit /login must not grow the Cookie header without bound."""
    for _ in range(auth_api.MAX_CONCURRENT_FLOWS + 3):
        await client.get("/api/auth/microsoft/login")
    assert len(flow_cookies(client)) <= auth_api.MAX_CONCURRENT_FLOWS


async def test_a_callback_without_state_is_rejected(client) -> None:
    await client.get("/api/auth/microsoft/login")
    response = await client.get("/api/auth/microsoft/callback", params={"code": "x"})
    assert response.status_code == 400


async def test_a_callback_with_an_unknown_state_is_rejected(client) -> None:
    await client.get("/api/auth/microsoft/login")
    response = await client.get(
        "/api/auth/microsoft/callback", params={"code": "x", "state": "never-issued"}
    )
    assert response.status_code == 400


async def test_reserved_scopes_are_not_passed_to_msal() -> None:
    """MSAL adds openid/profile/offline_access itself and errors if given them."""
    identity = set(ms_auth.delegated_scopes("identity"))
    mailbox = set(ms_auth.delegated_scopes("mailbox"))
    # Assert what survives too, or an empty list would satisfy the filter check.
    assert "User.Read" in identity
    assert "Mail.Read" in mailbox
    assert not {"openid", "profile", "offline_access"} & (identity | mailbox)


async def test_login_is_503_when_microsoft_is_not_configured(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MS_CLIENT_ID", "")
    assert (await client.get("/api/auth/microsoft/login")).status_code == 503
