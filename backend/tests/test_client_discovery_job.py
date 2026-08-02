"""The discovery worker job, end to end over a mock transport.

What these hold onto: enrichment is **additive** — a contact the client
already holds is never touched, a primary is set only into a vacancy, and a
re-run writes nothing the first run already wrote; new domains land in the
run's results and existing ones never do; a dead grant parks the run in
`failed` with words a recruiter can act on.
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from app.db.rls import tenant_session
from app.models.client import Client, ClientContact
from app.models.client_discovery import ClientDiscoveryRun
from app.services import ms_auth
from app.services.graph.client import GraphClient
from app.workers import discovery_jobs
from tests.conftest import cleanup_tenant, seed_tenant_with_user


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(payload), headers={"content-type": "application/json"}
    )


def _handler(request: httpx.Request) -> httpx.Response:
    """Two acme senders in the inbox, one reply out, one new company."""
    if "sentitems" in request.url.path.lower():
        return _json(
            {
                "value": [
                    {
                        "toRecipients": [
                            {
                                "emailAddress": {
                                    "address": "jane.lim@acme.com.sg",
                                    "name": "Jane Lim",
                                }
                            }
                        ],
                        "sentDateTime": "2026-08-01T08:00:00Z",
                    }
                ]
            }
        )
    return _json(
        {
            "value": [
                {
                    "from": {
                        "emailAddress": {
                            "address": "jane.lim@acme.com.sg",
                            "name": "Jane Lim",
                        }
                    },
                    "receivedDateTime": "2026-07-30T10:00:00Z",
                },
                {
                    "from": {
                        "emailAddress": {
                            "address": "tom@globex.com",
                            "name": "Tom Chen",
                        }
                    },
                    "receivedDateTime": "2026-07-29T10:00:00Z",
                },
            ]
        }
    )


@pytest.fixture
def graph(monkeypatch):
    """The job's Graph calls answered by `_handler`; the token exchange faked."""

    async def _token(tenant_id, user_id) -> str:
        return "token"

    monkeypatch.setattr(discovery_jobs.ms_auth, "access_token_for_user", _token)
    monkeypatch.setattr(
        discovery_jobs,
        "graph_client",
        lambda token: GraphClient(token, transport=httpx.MockTransport(_handler)),
    )


@pytest.fixture
async def agency():
    tenant_id, user_id = await seed_tenant_with_user()
    yield tenant_id, user_id
    await cleanup_tenant(tenant_id)


async def _make_run(tenant_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        session.add(
            ClientDiscoveryRun(
                id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                status=ClientDiscoveryRun.PENDING,
                lookback_days=90,
            )
        )
    return run_id


async def _make_client(tenant_id: uuid.UUID, domain: str) -> uuid.UUID:
    client_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        session.add(
            Client(
                id=client_id,
                tenant_id=tenant_id,
                name=domain,
                name_normalized=domain,
                email_domain=domain,
                status=Client.CONFIRMED,
            )
        )
    return client_id


async def _run_job(tenant_id: uuid.UUID, run_id: uuid.UUID) -> ClientDiscoveryRun:
    await discovery_jobs.run_client_discovery(
        {}, tenant_id=str(tenant_id), run_id=str(run_id)
    )
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                select(ClientDiscoveryRun).where(ClientDiscoveryRun.id == run_id)
            )
        ).scalar_one()


async def _contacts(tenant_id: uuid.UUID, client_id: uuid.UUID) -> list[ClientContact]:
    async with tenant_session(tenant_id) as session:
        return list(
            (
                await session.execute(
                    select(ClientContact)
                    .where(ClientContact.client_id == client_id)
                    .order_by(ClientContact.email)
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_scan_enriches_existing_clients_and_lists_only_new_domains(
    agency, graph
):
    tenant_id, user_id = agency
    acme = await _make_client(tenant_id, "acme.com.sg")
    run_id = await _make_run(tenant_id, user_id)

    run = await _run_job(tenant_id, run_id)

    assert run.status == ClientDiscoveryRun.DONE
    assert run.inbox_scanned == 2 and run.sent_scanned == 1
    # acme was already a client: enriched, and absent from the results.
    assert run.clients_enriched == 1 and run.contacts_added == 1
    assert [entry["domain"] for entry in run.results] == ["globex.com"]
    assert run.results[0]["contacts"][0]["email"] == "tom@globex.com"

    contacts = await _contacts(tenant_id, acme)
    assert [(c.email, c.name, c.is_primary) for c in contacts] == [
        ("jane.lim@acme.com.sg", "Jane Lim", True)
    ]

    # The client's last_seen_at moved forward to what the scan observed.
    async with tenant_session(tenant_id) as session:
        seen = (
            await session.execute(
                text("SELECT last_seen_at FROM clients WHERE id = :id"), {"id": acme}
            )
        ).scalar_one()
    assert seen is not None and seen.year == 2026


@pytest.mark.asyncio
async def test_a_rerun_adds_nothing_the_first_run_already_wrote(agency, graph):
    tenant_id, user_id = agency
    acme = await _make_client(tenant_id, "acme.com.sg")
    first = await _run_job(tenant_id, await _make_run(tenant_id, user_id))
    assert first.contacts_added == 1

    second = await _run_job(tenant_id, await _make_run(tenant_id, user_id))
    assert second.status == ClientDiscoveryRun.DONE
    assert second.contacts_added == 0 and second.clients_enriched == 0
    assert len(await _contacts(tenant_id, acme)) == 1


@pytest.mark.asyncio
async def test_an_existing_primary_is_never_displaced(agency, graph):
    tenant_id, user_id = agency
    acme = await _make_client(tenant_id, "acme.com.sg")
    async with tenant_session(tenant_id) as session:
        session.add(
            ClientContact(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_id=acme,
                name="Existing Primary",
                # Mixed case on purpose: matching is case-insensitive, and this
                # is a different person from jane.lim anyway.
                email="Boss@Acme.com.sg",
                is_primary=True,
            )
        )

    await _run_job(tenant_id, await _make_run(tenant_id, user_id))

    contacts = await _contacts(tenant_id, acme)
    primaries = [c for c in contacts if c.is_primary]
    assert [c.name for c in primaries] == ["Existing Primary"]
    assert {c.email for c in contacts} == {"Boss@Acme.com.sg", "jane.lim@acme.com.sg"}


@pytest.mark.asyncio
async def test_a_case_insensitive_duplicate_is_not_inserted_twice(agency, graph):
    tenant_id, user_id = agency
    acme = await _make_client(tenant_id, "acme.com.sg")
    async with tenant_session(tenant_id) as session:
        session.add(
            ClientContact(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_id=acme,
                name="Jane, as typed by hand",
                email="Jane.Lim@ACME.com.sg",
                is_primary=False,
            )
        )

    run = await _run_job(tenant_id, await _make_run(tenant_id, user_id))

    contacts = await _contacts(tenant_id, acme)
    assert len(contacts) == 1  # the hand-typed row, untouched
    assert contacts[0].name == "Jane, as typed by hand"
    assert run.contacts_added == 0
    # Jane is the domain's top sender and no primary existed, so the existing
    # row was promoted into the vacancy rather than duplicated.
    assert contacts[0].is_primary


@pytest.mark.asyncio
async def test_a_dead_grant_parks_the_run_in_failed(agency, graph, monkeypatch):
    tenant_id, user_id = agency

    async def _refused(tenant, user) -> str:
        raise ms_auth.MailboxNotAuthorised("refresh token rejected")

    monkeypatch.setattr(discovery_jobs.ms_auth, "access_token_for_user", _refused)

    run = await _run_job(tenant_id, await _make_run(tenant_id, user_id))
    assert run.status == ClientDiscoveryRun.FAILED
    assert "Reconnect" in run.error


@pytest.mark.asyncio
async def test_a_finished_run_is_never_rerun(agency, graph):
    tenant_id, user_id = agency
    run_id = await _make_run(tenant_id, user_id)
    async with tenant_session(tenant_id) as session:
        record = await session.get(ClientDiscoveryRun, run_id)
        record.status = ClientDiscoveryRun.DONE
        record.results = []

    run = await _run_job(tenant_id, run_id)
    assert run.status == ClientDiscoveryRun.DONE
    assert run.inbox_scanned == 0  # untouched — the replay changed nothing
