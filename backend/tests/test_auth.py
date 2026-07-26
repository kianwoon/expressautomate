"""Microsoft sign-in end to end, with MSAL mocked out.

Nothing here touches the network: `ms_auth.begin_login` / `complete_login` are
the only two places the real MSAL client is used, so replacing them isolates
the whole flow. What is *not* mocked is the database — provisioning a tenant
happens before any tenant context exists, and RLS is the part most likely to
break silently, so those statements run for real.
"""

import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.api import auth as auth_api
from app.core import crypto
from app.core.config import settings
from app.core.crypto import decrypt
from app.db.rls import tenant_session
from app.main import app
from app.services import ms_auth

AUTHORIZE_HOST = "https://login.microsoftonline.com"


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
def fake_msal(monkeypatch) -> None:
    """A believable flow dict and token response, with no MSAL client built."""

    def begin_login() -> dict:
        state = uuid.uuid4().hex
        return {
            "state": state,
            "code_verifier": uuid.uuid4().hex,
            "nonce": uuid.uuid4().hex,
            "redirect_uri": settings.MS_REDIRECT_URI,
            "scope": ms_auth.delegated_scopes(),
            "auth_uri": f"{AUTHORIZE_HOST}/organizations/oauth2/v2.0/authorize?state={state}",
        }

    monkeypatch.setattr(ms_auth, "begin_login", begin_login)


def token_response(tid: str, oid: str, email: str, name: str = "Rachel Tan") -> dict:
    return {
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "scope": " ".join(ms_auth.delegated_scopes()),
        "id_token_claims": {"tid": tid, "oid": oid, "preferred_username": email, "name": name},
    }


async def sign_in(client: httpx.AsyncClient, monkeypatch, result: dict) -> httpx.Response:
    login = await client.get("/auth/microsoft/login")
    assert login.status_code == 307
    monkeypatch.setattr(ms_auth, "complete_login", lambda flow, params: result)
    return await client.get("/auth/microsoft/callback", params={"code": "any-code"})


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
    response = await client.get("/auth/microsoft/login")
    assert response.status_code == 307
    assert response.headers["location"].startswith(f"{AUTHORIZE_HOST}/")
    assert "authorize" in response.headers["location"]
    assert auth_api.FLOW_COOKIE in response.cookies


async def test_login_does_not_leak_the_flow_secrets_into_the_cookie(client) -> None:
    """The cookie is encrypted, not merely signed — the PKCE verifier is secret."""
    response = await client.get("/auth/microsoft/login")
    sealed = response.cookies[auth_api.FLOW_COOKIE]
    assert auth_api._open_flow(sealed)["code_verifier"] not in sealed


async def test_callback_creates_the_tenant_and_user(client, monkeypatch, cleanup) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    response = await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    assert response.status_code == 307
    assert response.headers["location"] == settings.FRONTEND_ORIGIN

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

    await client.get("/auth/microsoft/login")
    monkeypatch.setattr(ms_auth, "complete_login", explode)
    response = await client.get("/auth/microsoft/callback", params={"code": "replayed"})
    assert response.status_code == 400


def test_a_placeholder_encryption_key_is_not_a_real_fernet_key() -> None:
    """In production such a key is refused; CI's placeholder still derives one."""
    assert not crypto._is_real_fernet_key("changeme")
    assert not crypto._is_real_fernet_key(settings.TOKEN_ENCRYPTION_KEY)
    assert crypto._is_real_fernet_key(Fernet.generate_key().decode())


def test_production_refuses_a_derived_encryption_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production")
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
    assert (await client.get("/auth/me")).status_code == 401


async def test_me_returns_the_signed_in_user(client, monkeypatch, cleanup) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    body = (await client.get("/auth/me")).json()
    assert body["email"] == "rachel@agency-a.sg"
    assert body["tenant_id"] == tid
    assert body["role"] == "recruiter"


async def test_logout_clears_the_session(client, monkeypatch, cleanup) -> None:
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    assert (await client.post("/auth/logout")).status_code == 200
    assert (await client.get("/auth/me")).status_code == 401


async def test_me_rejects_a_forged_cookie(client) -> None:
    client.cookies.set(auth_api.SESSION_COOKIE, "not-a-signed-value")
    assert (await client.get("/auth/me")).status_code == 401


async def test_callback_without_a_flow_cookie_is_rejected(client) -> None:
    assert (await client.get("/auth/microsoft/callback", params={"code": "x"})).status_code == 400


async def test_reserved_scopes_are_not_passed_to_msal() -> None:
    """MSAL adds openid/profile/offline_access itself and errors if given them."""
    assert not {"openid", "profile", "offline_access"} & set(ms_auth.delegated_scopes())


async def test_login_is_503_when_microsoft_is_not_configured(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MS_CLIENT_ID", "")
    assert (await client.get("/auth/microsoft/login")).status_code == 503
