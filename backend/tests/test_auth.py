"""Microsoft sign-in end to end, with MSAL mocked out.

Nothing here touches the network: `ms_auth.begin_login` / `complete_login` are
the only two places the real MSAL client is used, so replacing them isolates
the whole flow. What is *not* mocked is the database — provisioning a tenant
happens before any tenant context exists, and RLS is the part most likely to
break silently, so those statements run for real.
"""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.api import auth as auth_api
from app.api import mailbox as mailbox_api
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
    # Unset in CI, so the reserved-scope filter would otherwise be tested
    # against an empty list — vacuously true. Split exactly as production is:
    # the mailbox permission is NOT in the identity set, which is the whole
    # point of the split.
    monkeypatch.setattr(
        settings, "MS_IDENTITY_SCOPES", "openid profile email User.Read offline_access"
    )
    monkeypatch.setattr(settings, "MS_MAILBOX_SCOPES", "Mail.Read")


@pytest.fixture(autouse=True)
def fake_msal(monkeypatch) -> None:
    """A believable flow dict and token response, with no MSAL client built."""

    def begin_login(scopes: list[str], prompt: str | None = None) -> dict:
        state = uuid.uuid4().hex
        # Real MSAL puts `prompt` in the authorize query only when one is
        # given, and always puts the scopes there; mirroring both is what lets
        # a test read them back off the redirect instead of reaching into the
        # mock.
        query = f"state={state}&scope={quote(' '.join(scopes))}"
        if prompt:
            query += f"&prompt={prompt}"
        return {
            "state": state,
            "code_verifier": uuid.uuid4().hex,
            "nonce": uuid.uuid4().hex,
            "redirect_uri": settings.MS_REDIRECT_URI,
            "scope": scopes,
            "auth_uri": f"{AUTHORIZE_HOST}/organizations/oauth2/v2.0/authorize?{query}",
        }

    monkeypatch.setattr(ms_auth, "begin_login", begin_login)


def scopes_of(response: httpx.Response) -> set[str]:
    """The scopes an authorize redirect is asking Microsoft for."""
    return set(parse_qs(urlparse(response.headers["location"]).query)["scope"][0].split())


def token_response(
    tid: str,
    oid: str,
    email: str,
    name: str = "Rachel Tan",
    scopes: list[str] | None = None,
) -> dict:
    """A token response. Identity-only by default — that is what sign-in gets."""
    return {
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "scope": " ".join(ms_auth.identity_scopes() if scopes is None else scopes),
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


async def connect_mailbox(
    client: httpx.AsyncClient, monkeypatch, result: dict
) -> httpx.Response:
    """Walk the separate mailbox-consent round trip, for an already-signed-in user."""
    start = await client.get("/api/auth/microsoft/connect-mailbox")
    assert start.status_code == 307
    monkeypatch.setattr(ms_auth, "complete_login", lambda flow, params: result)
    return await client.get(
        "/api/auth/microsoft/callback",
        params={"code": "any-code", "state": state_of(start)},
    )


async def start_ingestion(
    client: httpx.AsyncClient, monkeypatch, window: str = "30d"
) -> httpx.Response:
    """Choose a period, which is what actually provisions the mailbox.

    Consent no longer does this (§6.2), so every test that needs a mailbox row
    has to say how far back the user asked to read — the same thing the
    dashboard makes them say.
    """

    async def _enqueue(name, **kwargs):
        return True

    monkeypatch.setattr(mailbox_api, "enqueue", _enqueue)
    return await client.post("/api/mailbox/ingest", json={"window": window})


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


async def test_login_asks_for_no_prompt_by_default(client) -> None:
    """An ordinary sign-in must not make everyone click through the picker."""
    response = await client.get("/api/auth/microsoft/login")
    assert "prompt" not in parse_qs(urlparse(response.headers["location"]).query)


async def test_login_forwards_select_account_so_an_account_can_be_switched(client) -> None:
    """Without this parameter Microsoft silently reuses the live SSO session,
    which is exactly what makes a second account unreachable."""
    response = await client.get(
        "/api/auth/microsoft/login", params={"prompt": "select_account"}
    )
    assert response.status_code == 307
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["prompt"] == ["select_account"]


async def test_login_rejects_an_unlisted_prompt(client) -> None:
    """Rejected, not ignored: the value reaches the authorize URL."""
    response = await client.get("/api/auth/microsoft/login", params={"prompt": "none"})
    assert response.status_code == 400


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

    assert auth_api._open_flow(sealed)["msal"]["code_verifier"] not in sealed
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


async def test_signing_in_again_switches_the_session_to_the_new_account(
    client, monkeypatch, cleanup
) -> None:
    """Switching accounts must replace the session, not stack a second one.

    The picker (`?prompt=select_account`) is only half of switching; the other
    half is that whoever comes back from Microsoft becomes the signed-in user,
    even though a valid session for the previous account is still in the jar.
    """
    personal, work = str(uuid.uuid4()), str(uuid.uuid4())
    cleanup.extend([uuid.UUID(personal), uuid.UUID(work)])

    await sign_in(client, monkeypatch, token_response(personal, uuid.uuid4().hex, "rach@live.com"))
    await sign_in(client, monkeypatch, token_response(work, uuid.uuid4().hex, "rachel@agency.sg"))

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "rachel@agency.sg"
    assert body["tenant"]["id"] == work


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
    # The first user to sign in for a new tenant becomes the owner, since they set up the agency
    # and are the only one who may delete candidate personal data.
    assert body["user"]["role"] == "owner"
    assert body["tenant"]["id"] == tid
    assert body["tenant"]["name"] == "agency-a.sg"
    # The dashboard is built against exactly these keys.
    assert set(body) == {"user", "tenant", "mailbox"}


async def test_signing_in_alone_does_not_count_as_a_connected_mailbox(
    client, monkeypatch, cleanup
) -> None:
    """The regression the scope split could most easily introduce.

    Sign-in asks for `offline_access`, so a refresh token — and therefore a
    token row — exists for every signed-in user. Reading that row's existence
    as "mailbox connected", which is what the endpoint used to do, would tell
    someone their mail was hooked up when nothing had been granted to read it.
    """
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["provider"] == "microsoft"
    assert mailbox["connected"] is False
    assert "Mail.Read" not in mailbox["scopes"]


async def test_me_reports_a_connected_mailbox_with_its_scopes(
    client, monkeypatch, cleanup
) -> None:
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg",
                                                              scopes=ms_auth.mailbox_scopes()))

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["provider"] == "microsoft"
    assert mailbox["connected"] is True
    assert mailbox["scopes"] == ms_auth.mailbox_scopes()
    # Connected, but not yet ingesting: the subscription is created by the
    # `recreate_subscription` job, which does not run in this test. Reporting
    # otherwise would tell the user their mail was flowing before it was.
    assert mailbox["ingestion_active"] is False


async def test_me_reports_a_dead_grant_instead_of_claiming_connected(
    client, monkeypatch, cleanup
) -> None:
    """A mailbox in `needs_reauth` must not read as connected.

    `connected` comes from the stored token's scope, which survives the grant
    going bad — so on its own it would tell someone their mail was flowing
    while ingestion had stopped. That is the failure the whole recovery layer
    exists to surface; the dashboard must not hide it.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )
    await start_ingestion(client, monkeypatch)

    async with tenant_session(uuid.UUID(tid)) as session:
        await session.execute(
            text("UPDATE mailboxes SET status = 'needs_reauth'")
        )

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["status"] == "needs_reauth"
    assert mailbox["ingestion_active"] is False


async def test_me_counts_only_what_was_actually_ingested(
    client, monkeypatch, cleanup
) -> None:
    """Numbers on the dashboard are read, never inferred (§15).

    A count that came from anywhere but the rows themselves would be the one
    thing this product promises not to do.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )
    await start_ingestion(client, monkeypatch)

    before = (await client.get("/api/auth/me")).json()["mailbox"]
    assert before["ingested"] == {
        "total": 0,
        "in_progress": 0,
        "awaiting_extraction": 0,
        "emails_extracted": 0,
        "opportunities": 0,
    }
    assert before["newest_received"] is None

    async with tenant_session(uuid.UUID(tid)) as session:
        mailbox_id = (
            await session.execute(text("SELECT id FROM mailboxes"))
        ).scalar_one()
        # One per bucket, so the two waits cannot be conflated: `pending` is
        # being fetched now, `fetched` is stored and waiting on extraction.
        for n, status in enumerate(["extracted", "fetched", "skipped", "pending"]):
            await session.execute(
                text(
                    "INSERT INTO email_messages"
                    " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
                    "  received_datetime)"
                    " VALUES (:id, :t, :m, :g, :s, now() - make_interval(days => :d))"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": uuid.UUID(tid),
                    "m": mailbox_id,
                    "g": f"MSG-{n}",
                    "s": status,
                    "d": n,
                },
            )

    # Two vacancies from the ONE extracted email — the case that broke the
    # dashboard: it showed "3 job orders found" above a table of 7 because the
    # headline counted emails and the table counted roles.
    async with tenant_session(uuid.UUID(tid)) as session:
        extracted_email = (
            await session.execute(
                text(
                    "SELECT id FROM email_messages WHERE processing_status = 'extracted'"
                )
            )
        ).scalar_one()
        for title in ("Clinic Assistant", "Finance Officer"):
            await session.execute(
                text(
                    "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                    " job_title_raw, review_status, quality_state)"
                    " VALUES (:i, :t, :e, :title, 'ready', 'verified')"
                ),
                {
                    "i": uuid.uuid4(),
                    "t": uuid.UUID(tid),
                    "e": extracted_email,
                    "title": title,
                },
            )

    after = (await client.get("/api/auth/me")).json()["mailbox"]
    assert after["ingested"] == {
        # Still 4. The opportunities are counted by a subquery, not a join —
        # joining them would repeat this email once per vacancy and inflate
        # every other figure here.
        "total": 4,
        # Only the `pending` row. A `fetched` row is not "processing" — that
        # claim put "usually seconds each" under 82 emails waiting on a
        # release that had not shipped.
        "in_progress": 1,
        "awaiting_extraction": 1,
        "emails_extracted": 1,
        "opportunities": 2,
    }
    assert after["newest_received"] is not None
    assert after["oldest_received"] < after["newest_received"]


async def test_me_surfaces_the_worst_state_across_several_mailboxes(
    client, monkeypatch, cleanup
) -> None:
    """A user can own more than one mailbox — the constraint is per folder.

    Reporting only the first would show a healthy dashboard while a second had
    stopped, which is the silence this endpoint exists to break. Whatever needs
    attention outranks whatever is working.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )
    await start_ingestion(client, monkeypatch)

    async with tenant_session(uuid.UUID(tid)) as session:
        user_id = (await session.execute(text("SELECT id FROM users"))).scalar_one()
        # A second folder on the same account, which then goes bad.
        await session.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, folder_id, scope, status,"
                "  retention_months)"
                " VALUES (:id, :t, :u, :oid, 'jobs', 'folder', 'needs_reauth', 24)"
            ),
            {"id": uuid.uuid4(), "t": uuid.UUID(tid), "u": user_id, "oid": oid},
        )

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["status"] == "needs_reauth", "the broken one must not be hidden"


async def test_me_reports_no_counts_before_a_mailbox_exists(
    client, monkeypatch, cleanup
) -> None:
    """Signing in alone provisions nothing, so there is nothing to count."""
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "r@a.sg"))

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["status"] is None
    assert mailbox["ingested"]["total"] == 0


async def test_consent_waits_for_the_user_to_choose_a_period(
    client, monkeypatch, cleanup
) -> None:
    """Consent grants permission; it does not choose how much mail to import.

    Provisioning here is what used to pull ninety days of someone's mail on the
    strength of a button they pressed to grant a permission. Now the grant is
    all that lands, and `/auth/me` says a choice is outstanding (§6.2).
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )

    async with tenant_session(uuid.UUID(tid)) as session:
        count = (await session.execute(text("SELECT count(*) FROM mailboxes"))).scalar_one()
    assert count == 0, "consent must not provision a mailbox"

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    assert mailbox["connected"] is True
    assert mailbox["awaiting_period"] is True
    assert mailbox["status"] is None


async def test_choosing_a_period_provisions_the_mailbox(client, monkeypatch, cleanup) -> None:
    """The chosen window is what gets written, and only then does work start."""
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(mailbox_api, "enqueue", _enqueue)

    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )

    response = await client.post("/api/mailbox/ingest", json={"window": "7d"})
    assert response.status_code == 200

    async with tenant_session(uuid.UUID(tid)) as session:
        row = (
            await session.execute(
                text(
                    "SELECT ms_user_id, scope, folder_id, status, initial_sync_from"
                    " FROM mailboxes"
                )
            )
        ).one()
    assert row.ms_user_id == oid
    assert row.scope == "whole_inbox"
    assert row.status == "active"
    # Seven days back, not the 90-day maximum the old default would have used.
    age = datetime.now(UTC) - row.initial_sync_from
    assert timedelta(days=6, hours=23) < age < timedelta(days=7, hours=1)

    assert [name for name, _ in queued] == ["recreate_subscription", "backfill_mailbox_job"]
    for _, kwargs in queued:
        assert kwargs["tenant_id"] == tid

    # Asked twice, it refuses rather than re-walking history or silently
    # doing nothing.
    assert (await client.post("/api/mailbox/ingest", json={"window": "30d"})).status_code == 409


async def test_preview_names_the_missing_setting_rather_than_500ing(
    client, monkeypatch, cleanup
) -> None:
    """Found in production. GRAPH_BASE_URL was set on both workers and not on
    the web service, so the first web-service Graph call built a hostless URL
    and died inside httpx's transport — a bare 500 for a one-line deployment
    gap. 503 naming the setting is the answerable version.

    Checked after the session, not before: an anonymous caller has no business
    learning what this deployment is missing.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    monkeypatch.setattr(settings, "GRAPH_BASE_URL", "")

    anonymous = await client.get("/api/mailbox/preview")
    assert anonymous.status_code == 401, "configuration is not for strangers"

    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )

    response = await client.get("/api/mailbox/preview")
    assert response.status_code == 503
    assert "GRAPH_BASE_URL" in response.json()["detail"]

    # The path that does not touch Graph in this process is not blocked by it:
    # its work runs on the workers, which carry their own configuration.
    assert (await client.post("/api/mailbox/ingest", json={"window": "now"})).status_code == 200


async def test_reconnecting_revives_a_broken_mailbox(client, monkeypatch, cleanup) -> None:
    """A `needs_reauth` mailbox must come back when the user reconnects.

    Found in review. Once consent stopped provisioning, nothing flipped the row
    back to active: `/mailbox/ingest` refuses a mailbox that already exists, so
    re-consenting stored a working token beside a row no sweep would ever look
    at again — the dashboard offering "Reconnect" forever to someone who just
    had.

    The window is not re-asked and the backfill is not re-run: the user chose
    that once, and history has not moved.
    """
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(auth_api, "enqueue", _enqueue)

    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    grant = token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes())
    await connect_mailbox(client, monkeypatch, grant)
    await start_ingestion(client, monkeypatch, window="7d")

    async with tenant_session(uuid.UUID(tid)) as session:
        await session.execute(text("UPDATE mailboxes SET status = 'needs_reauth'"))
        chosen = (
            await session.execute(text("SELECT initial_sync_from FROM mailboxes"))
        ).scalar_one()
    assert (await client.get("/api/auth/me")).json()["mailbox"]["status"] == "needs_reauth"

    queued.clear()
    await connect_mailbox(client, monkeypatch, grant)

    async with tenant_session(uuid.UUID(tid)) as session:
        row = (
            await session.execute(text("SELECT status, initial_sync_from FROM mailboxes"))
        ).one()
    assert row.status == "active"
    assert row.initial_sync_from == chosen, "reconnecting must not re-walk history"
    assert [name for name, _ in queued] == ["recreate_subscription"]

    after = (await client.get("/api/auth/me")).json()["mailbox"]
    assert after["status"] == "active"
    assert after["awaiting_period"] is False, "the period was chosen the first time"


async def test_reconnecting_revives_every_broken_mailbox(client, monkeypatch, cleanup) -> None:
    """A user can own several — the constraint is per folder — and one grant
    took them all down. Reviving one and calling it done leaves the dashboard
    reporting active while a second mailbox stays dark.

    allow-hardcode: SQL statements, not a phrase list — nothing is matched
    against these strings.
    """
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(auth_api, "enqueue", _enqueue)

    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    grant = token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes())
    await connect_mailbox(client, monkeypatch, grant)
    await start_ingestion(client, monkeypatch)

    async with tenant_session(uuid.UUID(tid)) as session:
        # A second folder, as the schema allows, then both broken together.
        await session.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id,"
                "  initial_sync_from, retention_months)"
                " SELECT :id, tenant_id, user_id, ms_user_id, scope, 'archive',"
                "  initial_sync_from, retention_months FROM mailboxes"
            ),
            {"id": uuid.uuid4()},
        )
        await session.execute(text("UPDATE mailboxes SET status = 'needs_reauth'"))

    queued.clear()
    await connect_mailbox(client, monkeypatch, grant)

    async with tenant_session(uuid.UUID(tid)) as session:
        states = (
            (await session.execute(text("SELECT status FROM mailboxes"))).scalars().all()
        )
    assert states == ["active", "active"]
    assert [name for name, _ in queued] == ["recreate_subscription"] * 2


async def test_reconnecting_a_healthy_mailbox_changes_nothing(
    client, monkeypatch, cleanup
) -> None:
    """Re-consenting when nothing is wrong must not churn a live subscription."""
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(auth_api, "enqueue", _enqueue)

    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    grant = token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes())
    await connect_mailbox(client, monkeypatch, grant)
    await start_ingestion(client, monkeypatch)

    queued.clear()
    await connect_mailbox(client, monkeypatch, grant)
    assert queued == []


async def test_ingestion_is_refused_without_the_mailbox_grant(
    client, monkeypatch, cleanup
) -> None:
    """Microsoft can return a token narrower than asked for. Provisioning on
    that would leave a mailbox nothing can read."""
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(mailbox_api, "enqueue", _enqueue)

    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    # Identity only — the mailbox scope was refused.
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.identity_scopes()),
    )

    assert (await client.post("/api/mailbox/ingest", json={"window": "7d"})).status_code == 403

    async with tenant_session(uuid.UUID(tid)) as session:
        count = (await session.execute(text("SELECT count(*) FROM mailboxes"))).scalar_one()
    assert count == 0
    assert queued == []


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


async def test_login_asks_only_for_the_identity_scopes(client) -> None:
    response = await client.get("/api/auth/microsoft/login")
    assert scopes_of(response) == set(ms_auth.identity_scopes())
    assert "Mail.Read" not in scopes_of(response)


async def test_connect_mailbox_asks_for_the_mailbox_scopes(
    client, monkeypatch, cleanup
) -> None:
    """Identity scopes are re-requested alongside, or the incremental consent
    would hand back a token narrower than the one already held."""
    tid = str(uuid.uuid4())
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    response = await client.get("/api/auth/microsoft/connect-mailbox")
    assert response.status_code == 307
    assert scopes_of(response) == set(ms_auth.mailbox_scopes())
    assert "Mail.Read" in scopes_of(response)


async def test_connect_mailbox_requires_a_session(client) -> None:
    """The consent is filed against a user, so there must be one first."""
    assert (await client.get("/api/auth/microsoft/connect-mailbox")).status_code == 401


async def test_connect_mailbox_does_not_change_who_is_signed_in(
    client, monkeypatch, cleanup
) -> None:
    """Granting a permission is not an authentication."""
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    before = (await client.get("/api/auth/me")).json()["user"]["id"]

    done = await connect_mailbox(
        client, monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )
    assert done.status_code == 307
    assert auth_api.SESSION_COOKIE not in done.cookies
    assert (await client.get("/api/auth/me")).json()["user"]["id"] == before


async def test_consenting_as_a_different_account_is_rejected(
    client, monkeypatch, cleanup
) -> None:
    """Microsoft will let the user approve as somebody else. Storing that token
    against the session user would file one person's mail credential under
    another person's row."""
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))

    # Same tenant, different person — the `oid` is the guard, not the address.
    response = await connect_mailbox(
        client, monkeypatch,
        token_response(tid, uuid.uuid4().hex, "sam@agency-a.sg",
                       scopes=ms_auth.mailbox_scopes()),
    )
    assert response.status_code == 400
    assert (await client.get("/api/auth/me")).json()["mailbox"]["connected"] is False


async def test_consenting_from_another_tenant_is_rejected(
    client, monkeypatch, cleanup
) -> None:
    tid, other = str(uuid.uuid4()), str(uuid.uuid4())
    cleanup.extend([uuid.UUID(tid), uuid.UUID(other)])
    await sign_in(client, monkeypatch, token_response(tid, uuid.uuid4().hex, "rachel@agency-a.sg"))

    response = await connect_mailbox(
        client, monkeypatch,
        token_response(other, uuid.uuid4().hex, "sam@agency-b.sg",
                       scopes=ms_auth.mailbox_scopes()),
    )
    assert response.status_code == 400
    assert (await client.get("/api/auth/me")).json()["mailbox"]["connected"] is False


async def test_signing_in_again_does_not_disconnect_a_connected_mailbox(
    client, monkeypatch, cleanup
) -> None:
    """A later sign-in carries an identity-only refresh token. Storing it over
    the mailbox one leaves a row claiming "connected" and a credential that
    cannot read mail — a silent breakage, which is the worst kind."""
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client, monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )

    async with tenant_session(uuid.UUID(tid)) as s:
        kept = (await s.execute(text("SELECT refresh_token_encrypted FROM ms_oauth_tokens"))
                ).scalar_one()

    result = token_response(tid, oid, "rachel@agency-a.sg")
    result["refresh_token"] = "an-identity-only-refresh-token"
    await sign_in(client, monkeypatch, result)

    assert (await client.get("/api/auth/me")).json()["mailbox"]["connected"] is True
    async with tenant_session(uuid.UUID(tid)) as s:
        still = (await s.execute(text("SELECT refresh_token_encrypted FROM ms_oauth_tokens"))
                 ).scalar_one()
    assert decrypt(still) == decrypt(kept) != "an-identity-only-refresh-token"


async def test_reconnecting_a_mailbox_rotates_the_refresh_token(
    client, monkeypatch, cleanup
) -> None:
    """The escape hatch from the downgrade guard.

    The guard refuses anything narrower than what is stored. Taken literally
    that is a trap: narrow the configured mailbox scopes, or have Microsoft
    grant a subset, and no later consent could ever satisfy it — the token
    could never be rotated and the only fix would be deleting the row by hand.
    The mailbox flow asks for everything, so what it gets back IS the grant.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client, monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )

    # Consent again, granting strictly less than before and a fresh token.
    again = token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.identity_scopes())
    again["refresh_token"] = "a-rotated-refresh-token"
    assert (await connect_mailbox(client, monkeypatch, again)).status_code == 307

    async with tenant_session(uuid.UUID(tid)) as s:
        stored = (await s.execute(text("SELECT refresh_token_encrypted FROM ms_oauth_tokens"))
                  ).scalar_one()
    assert decrypt(stored) == "a-rotated-refresh-token"
    # And the narrower grant is reported honestly, not remembered as connected.
    assert (await client.get("/api/auth/me")).json()["mailbox"]["connected"] is False


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
    scopes = set(ms_auth.mailbox_scopes())
    # Assert what survives too, or an empty list would satisfy the filter check.
    assert {"User.Read", "Mail.Read"} <= scopes
    assert not {"openid", "profile", "offline_access"} & scopes


async def test_sign_in_never_asks_for_the_mailbox_permission() -> None:
    """The whole point of the split: a tenant that refuses Mail.Read must still
    be able to sign its people in."""
    assert "Mail.Read" not in ms_auth.identity_scopes()
    assert "User.Read" in ms_auth.identity_scopes()


async def test_login_is_503_when_microsoft_is_not_configured(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MS_CLIENT_ID", "")
    assert (await client.get("/api/auth/microsoft/login")).status_code == 503


async def test_the_vacancy_count_covers_the_agency_not_just_your_own_mailbox(
    client, monkeypatch, cleanup
) -> None:
    """The headline must count what the table lists, and the table is agency-wide.

    Found in review. The count was scoped to the signed-in user's mailboxes
    while `GET /opportunities` returns everything the tenant has — so in an
    agency of several recruiters a colleague's vacancy appeared in the table
    and not in the number above it. That is the same contradiction this pair
    of fields was introduced to remove, one tier up, and it also hid the table
    completely from anyone whose own mailbox had found nothing.

    allow-hardcode: SQL fixtures.
    """
    tid, oid = str(uuid.uuid4()), uuid.uuid4().hex
    cleanup.append(uuid.UUID(tid))
    await sign_in(client, monkeypatch, token_response(tid, oid, "rachel@agency-a.sg"))
    await connect_mailbox(
        client,
        monkeypatch,
        token_response(tid, oid, "rachel@agency-a.sg", scopes=ms_auth.mailbox_scopes()),
    )
    await start_ingestion(client, monkeypatch)

    # A colleague in the same agency, with their own mailbox and one vacancy.
    async with tenant_session(uuid.UUID(tid)) as session:
        colleague, their_mailbox, their_email = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, ms_object_id, role)"
                " VALUES (:i, :t, :e, :o, 'recruiter')"
            ),
            {"i": colleague, "t": uuid.UUID(tid), "e": "sam@agency-a.sg", "o": uuid.uuid4().hex},
        )
        await session.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, retention_months)"
                " VALUES (:i, :t, :u, 'sam-ms', 'inbox', 'whole_inbox', 24)"
            ),
            {"i": their_mailbox, "t": uuid.UUID(tid), "u": colleague},
        )
        await session.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " processing_status) VALUES (:i, :t, :m, 'MSG-SAM', 'extracted')"
            ),
            {"i": their_email, "t": uuid.UUID(tid), "m": their_mailbox},
        )
        await session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_raw, review_status, quality_state)"
                " VALUES (:i, :t, :e, 'Payroll Executive', 'ready', 'verified')"
            ),
            {"i": uuid.uuid4(), "t": uuid.UUID(tid), "e": their_email},
        )

    mailbox = (await client.get("/api/auth/me")).json()["mailbox"]
    # Rachel's own mailbox produced nothing, but the agency has one vacancy and
    # the table will show it — so the number must too.
    assert mailbox["ingested"]["emails_extracted"] == 0, "none of Rachel's own mail"
    assert mailbox["ingested"]["opportunities"] == 1
