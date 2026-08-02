"""The client-discovery routes: gates, run lifecycle, and the create path.

Nothing here reaches Graph or Redis: `enqueue` is replaced at the point the
route imports it, and the scan itself is the worker's job (tested in
`test_client_discovery_job.py`). What these hold onto: the scan is gated on
the mailbox grant exactly as the preview is; one live run per user; results
are per-user and per-tenant; and Create writes `confirmed`/`manual` clients
with their contacts, answering per domain rather than all-or-nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api import client_discovery as discovery_api
from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models.client import Client, ClientContact
from app.models.client_discovery import ClientDiscoveryRun
from tests.conftest import (
    AdminSessionLocal,
    cleanup_tenant,
    seed_tenant_with_user,
    sign_in,
)


@pytest.fixture
async def agency():
    tenant_id, user_id = await seed_tenant_with_user()
    yield tenant_id, user_id
    await cleanup_tenant(tenant_id)


@pytest.fixture
async def other_agency():
    tenant_id, user_id = await seed_tenant_with_user()
    yield tenant_id, user_id
    await cleanup_tenant(tenant_id)


@pytest.fixture
def queued(monkeypatch):
    """Every job the route tried to enqueue. Redis is never touched."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(discovery_api, "enqueue", _enqueue)
    return jobs


@pytest.fixture
def enqueue_fails(monkeypatch):
    """Redis down: `enqueue` fails soft, exactly as `queue.py` does."""

    async def _enqueue(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(discovery_api, "enqueue", _enqueue)


async def _grant_mailbox(
    tenant_id: uuid.UUID, user_id: uuid.UUID, monkeypatch
) -> None:
    """What `_connected_user` checks: a grant covering the mailbox scopes and
    a Microsoft object id on the user."""
    monkeypatch.setattr(settings, "MS_MAILBOX_SCOPES", "Mail.Read")
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO ms_oauth_tokens"
                " (id, tenant_id, user_id, refresh_token_encrypted, scope)"
                " VALUES (:i, :t, :u, 'ciphertext', 'Mail.Read')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "u": user_id},
        )
        await session.execute(
            text("UPDATE users SET ms_object_id = :o WHERE id = :u"),
            {"o": f"oid-{user_id.hex[:8]}", "u": user_id},
        )
        await session.commit()


def _http(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, user_id, tenant_id)
    return client


def _entry(domain: str, contacts: list[dict] | None = None) -> dict:
    return {
        "domain": domain,
        "score": 12.0,
        "received": 3,
        "sent": 1,
        "unique_contacts": 1,
        "last_activity": "2026-08-01T08:00:00+00:00",
        "created": False,
        "contacts": contacts
        if contacts is not None
        else [
            {
                "email": f"jane@{domain}",
                "name": "Jane Lim",
                "inbound": 3,
                "outbound": 1,
                "last_activity": "2026-08-01T08:00:00+00:00",
            }
        ],
    }


async def _done_run(
    tenant_id: uuid.UUID, user_id: uuid.UUID, entries: list[dict]
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        session.add(
            ClientDiscoveryRun(
                id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                status=ClientDiscoveryRun.DONE,
                lookback_days=90,
                results=entries,
            )
        )
    return run_id


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_route_requires_a_session():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        assert (await http.post("/api/client-discovery/scan")).status_code == 401
        assert (await http.get("/api/client-discovery")).status_code == 401
        assert (
            await http.post(
                "/api/client-discovery/clients", json={"domains": ["acme.com"]}
            )
        ).status_code == 401


@pytest.mark.asyncio
async def test_scanning_needs_the_mailbox_grant(agency):
    """No grant on file: the same honest 403 the inbox preview gives."""
    tenant_id, user_id = agency
    async with _http(tenant_id, user_id) as http:
        response = await http.post("/api/client-discovery/scan")
    assert response.status_code == 403
    assert "mailbox" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_creates_a_run_and_queues_the_job(
    agency, queued, monkeypatch
):
    tenant_id, user_id = agency
    await _grant_mailbox(tenant_id, user_id, monkeypatch)

    async with _http(tenant_id, user_id) as http:
        response = await http.post("/api/client-discovery/scan")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert body["lookback_days"] == settings.CLIENT_DISCOVERY_LOOKBACK_DAYS
        assert body["results"] is None

        assert queued == [
            (
                "run_client_discovery",
                {"tenant_id": str(tenant_id), "run_id": body["id"]},
            )
        ]

        read_back = await http.get("/api/client-discovery")
        assert read_back.json()["run"]["id"] == body["id"]


@pytest.mark.asyncio
async def test_a_fresh_running_scan_refuses_a_second(agency, queued, monkeypatch):
    tenant_id, user_id = agency
    await _grant_mailbox(tenant_id, user_id, monkeypatch)

    async with _http(tenant_id, user_id) as http:
        first = await http.post("/api/client-discovery/scan")
        assert first.status_code == 202
        second = await http.post("/api/client-discovery/scan")
        assert second.status_code == 409
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_a_stale_running_scan_is_superseded(agency, queued, monkeypatch):
    """A worker died mid-run: the row must not block scanning forever."""
    tenant_id, user_id = agency
    await _grant_mailbox(tenant_id, user_id, monkeypatch)

    stale_id = uuid.uuid4()
    stale_created = datetime.now(UTC) - timedelta(
        minutes=settings.CLIENT_DISCOVERY_STALE_RUNNING_MINUTES + 1
    )
    async with tenant_session(tenant_id) as session:
        session.add(
            ClientDiscoveryRun(
                id=stale_id,
                tenant_id=tenant_id,
                user_id=user_id,
                status=ClientDiscoveryRun.RUNNING,
                lookback_days=90,
            )
        )
    async with AdminSessionLocal() as session:
        await session.execute(
            text("UPDATE client_discovery_runs SET created_at = :c WHERE id = :i"),
            {"c": stale_created, "i": stale_id},
        )
        await session.commit()

    async with _http(tenant_id, user_id) as http:
        response = await http.post("/api/client-discovery/scan")
        assert response.status_code == 202

    async with tenant_session(tenant_id) as session:
        runs = list(
            (await session.execute(select(ClientDiscoveryRun.id))).scalars()
        )
    # The stale run is gone — one working set per user, not a history.
    assert runs == [uuid.UUID(response.json()["id"])]


@pytest.mark.asyncio
async def test_a_lost_enqueue_marks_the_run_failed(
    agency, enqueue_fails, monkeypatch
):
    """Nothing sweeps discovery runs, so a lost enqueue must not leave a
    `pending` row nothing will ever claim."""
    tenant_id, user_id = agency
    await _grant_mailbox(tenant_id, user_id, monkeypatch)

    async with _http(tenant_id, user_id) as http:
        response = await http.post("/api/client-discovery/scan")
        assert response.status_code == 202
        assert response.json()["status"] == "failed"
        assert response.json()["error"]


@pytest.mark.asyncio
async def test_runs_are_private_to_their_user_and_tenant(agency, other_agency):
    tenant_id, user_id = agency
    await _done_run(tenant_id, user_id, [_entry("acme.com")])

    # A colleague in the same agency: their page shows their own (absent) run,
    # not mine — the scan read MY mailbox.
    colleague = uuid.uuid4()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role)"
                " VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": colleague, "t": tenant_id, "e": f"{colleague.hex[:8]}@example.test"},
        )
        await session.commit()
    async with _http(tenant_id, colleague) as http:
        assert (await http.get("/api/client-discovery")).json()["run"] is None

    # Another agency entirely: RLS answers with nothing.
    other_tenant, other_user = other_agency
    async with _http(other_tenant, other_user) as http:
        assert (await http.get("/api/client-discovery")).json()["run"] is None


# ---------------------------------------------------------------------------
# Creating clients from a run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_requires_a_finished_run(agency):
    tenant_id, user_id = agency
    async with _http(tenant_id, user_id) as http:
        response = await http.post(
            "/api/client-discovery/clients", json={"domains": ["acme.com"]}
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_create_writes_confirmed_clients_with_their_contacts(agency):
    tenant_id, user_id = agency
    await _done_run(tenant_id, user_id, [_entry("acme.com.sg"), _entry("globex.com")])

    async with _http(tenant_id, user_id) as http:
        response = await http.post(
            "/api/client-discovery/clients",
            json={"domains": ["acme.com.sg", "not-scanned.com"]},
        )
        assert response.status_code == 200
        outcomes = {o["domain"]: o for o in response.json()["results"]}
        assert outcomes["acme.com.sg"]["outcome"] == "created"
        assert outcomes["acme.com.sg"]["contacts_added"] == 1
        # A domain the run never ranked is named, not silently skipped.
        assert outcomes["not-scanned.com"]["outcome"] == "not_in_scan"

        # The run remembers what was acted on, so a reload does not offer the
        # same row again.
        run = (await http.get("/api/client-discovery")).json()["run"]
        flags = {e["domain"]: e["created"] for e in run["results"]}
        assert flags == {"acme.com.sg": True, "globex.com": False}

    async with tenant_session(tenant_id) as session:
        client = (
            await session.execute(
                select(Client).where(Client.email_domain == "acme.com.sg")
            )
        ).scalar_one()
        # A person selected this company: confirmed and manual, exactly as
        # POST /clients creates one. Nobody is assigned — that is a separate,
        # explicit act everywhere in this app.
        assert client.status == Client.CONFIRMED
        assert client.source == Client.MANUAL
        assert client.name == "acme.com.sg"
        assert client.assigned_user_id is None
        contacts = list(
            (
                await session.execute(
                    select(ClientContact).where(ClientContact.client_id == client.id)
                )
            ).scalars()
        )
        assert [(c.email, c.name, c.is_primary) for c in contacts] == [
            ("jane@acme.com.sg", "Jane Lim", True)
        ]


@pytest.mark.asyncio
async def test_create_adopts_a_client_that_appeared_since_the_scan(agency):
    """The pipeline got there between scan and click: adopt, never crash."""
    tenant_id, user_id = agency
    await _done_run(tenant_id, user_id, [_entry("acme.com.sg")])
    async with tenant_session(tenant_id) as session:
        session.add(
            Client(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name="Acme",
                name_normalized="acme",
                email_domain="acme.com.sg",
                status=Client.UNCONFIRMED,
            )
        )

    async with _http(tenant_id, user_id) as http:
        response = await http.post(
            "/api/client-discovery/clients", json={"domains": ["acme.com.sg"]}
        )
        outcome = response.json()["results"][0]
        assert outcome["outcome"] == "already_existed"

    async with tenant_session(tenant_id) as session:
        client = (
            await session.execute(
                select(Client).where(Client.email_domain == "acme.com.sg")
            )
        ).scalar_one()
        # The user's selection IS the confirmation the pipeline was waiting
        # for — but only `unconfirmed` is promoted; the name a person may
        # have edited is left alone.
        assert client.status == Client.CONFIRMED
        assert client.name == "Acme"


@pytest.mark.asyncio
async def test_create_is_idempotent_for_contacts(agency):
    """A double-clicked Create must not write the same person twice."""
    tenant_id, user_id = agency
    await _done_run(tenant_id, user_id, [_entry("acme.com.sg")])

    async with _http(tenant_id, user_id) as http:
        first = await http.post(
            "/api/client-discovery/clients", json={"domains": ["acme.com.sg"]}
        )
        assert first.json()["results"][0]["contacts_added"] == 1
        second = await http.post(
            "/api/client-discovery/clients", json={"domains": ["acme.com.sg"]}
        )
        assert second.json()["results"][0]["outcome"] == "already_existed"
        assert second.json()["results"][0]["contacts_added"] == 0

    async with tenant_session(tenant_id) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM client_contacts WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert count == 1
