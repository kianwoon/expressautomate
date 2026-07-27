"""The list is a review queue before it is a directory.

Most rows in a young tenant are unconfirmed proposals, so the default view
excludes merged rows (which are no longer anyone's client) and the counts are
computed over the whole tenant rather than the page — a chip that shrank as
you paged would answer a different question than it appears to.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal

# Reuse, do not redefine: this is the same session cookie the real app reads.
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def agency_with_clients():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {"live": uuid.uuid4(), "merged": uuid.uuid4()}
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                "email_domain, status) VALUES (:i, :t, 'Acme', 'acme', 'acme.com', 'unconfirmed')"
            ),
            {"i": ids["live"], "t": tid},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status, "
                "merged_into_client_id) VALUES (:i, :t, 'Acme Old', 'acme old', 'merged', :w)"
            ),
            {"i": ids["merged"], "t": tid, "w": ids["live"]},
        )
        await s.commit()
    yield tid, uid, ids
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM client_mentions WHERE tenant_id = :t"), {"t": tid})
        await s.execute(
            text(
                "UPDATE clients SET status = 'unconfirmed', merged_into_client_id = NULL "
                "WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
        await s.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_list_hides_merged_rows_by_default(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]


async def test_the_status_filter_is_the_review_queue(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?status=unconfirmed")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]
    assert body["counts"]["unconfirmed"] == 1


async def test_confirming_is_the_only_way_a_client_becomes_confirmed(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['live']}/confirm")).status_code == 200
        body = (await http.get(f"/api/clients/{ids['live']}")).json()
    assert body["status"] == "confirmed"


async def test_unmerge_restores_a_wrongly_merged_client(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['merged']}/unmerge")).status_code == 200
        body = (await http.get(f"/api/clients/{ids['merged']}")).json()
    assert body["status"] == "unconfirmed"
    assert body["merged_into_client_id"] is None


async def test_a_client_cannot_be_merged_into_itself(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/clients/{ids['live']}/merge", json={"target_id": str(ids["live"])}
        )
    assert r.status_code == 400


async def test_one_agency_never_sees_anothers_clients(agency_with_clients) -> None:
    tid, uid, _ = agency_with_clients
    other_tid, other_uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": other_tid, "n": f"other-{other_tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": other_uid, "t": other_tid, "e": f"o{other_uid.hex[:6]}@other.sg"},
        )
        await s.commit()
    try:
        async with await _client_for(other_tid, other_uid) as http:
            body = (await http.get("/api/clients")).json()
        assert body["items"] == []
        assert body["total"] == 0
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": other_tid})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tid})
            await s.commit()
