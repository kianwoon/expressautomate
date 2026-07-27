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


async def test_merge_drops_a_mention_that_collides_on_the_target(agency_with_clients) -> None:
    """Re-merging after unmerge + reprocess must not 500 on a duplicate mention.

    A already holds a mention for message M (re-attached after an earlier
    unmerge). Merging A into B again would try to move that mention onto B,
    which already has its own mention for M — a unique violation unless the
    collision is handled deliberately.
    """
    # A NULL email_message_id collides too: the unique constraint declares
    # NULLS NOT DISTINCT, so two NULL-message mentions on one client already
    # collide with each other, and the same is true across a merge. This lets
    # the test reproduce the collision without standing up an email_messages
    # (and mailboxes) fixture just to get two rows with equal, non-null ids.
    tid, uid, ids = agency_with_clients
    async with AdminSessionLocal() as s:
        # ids['live'] (the merge target) already has a mention with no message.
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, "
                "matched_by) VALUES (:i, :t, :c, NULL, 'email_domain')"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": ids["live"]},
        )
        # ids['merged'] (the loser, currently already merged into 'live') also
        # picked up its own no-message mention. Bring it back to a mergeable
        # state first.
        await s.execute(
            text(
                "UPDATE clients SET status = 'unconfirmed', merged_into_client_id = NULL "
                "WHERE id = :i"
            ),
            {"i": ids["merged"]},
        )
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, "
                "matched_by) VALUES (:i, :t, :c, NULL, 'email_domain')"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": ids["merged"]},
        )
        await s.commit()

    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/clients/{ids['merged']}/merge", json={"target_id": str(ids["live"])}
        )
    assert r.status_code == 200

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT client_id FROM client_mentions WHERE tenant_id = :t "
                    "AND email_message_id IS NULL"
                ),
                {"t": tid},
            )
        ).fetchall()
    # Exactly one no-message mention survives, on the surviving client.
    assert [row[0] for row in rows] == [ids["live"]]


async def test_unmerge_refuses_when_the_domain_was_reclaimed(agency_with_clients) -> None:
    """If a new live client took over the freed domain, unmerge must not 500.

    Merging 'live' (acme.com) into some other client frees the domain slot,
    letting a new client C claim acme.com. Unmerging 'live' afterward would
    put two live rows on acme.com, violating uq_clients_tenant_domain, unless
    the endpoint handles the collision.
    """
    tid, uid, ids = agency_with_clients
    other_target = uuid.uuid4()
    new_claimant = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Other', 'other', 'unconfirmed')"
            ),
            {"i": other_target, "t": tid},
        )
        await s.commit()

    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/clients/{ids['live']}/merge", json={"target_id": str(other_target)}
        )
    assert r.status_code == 200

    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                "email_domain, status) VALUES (:i, :t, 'Acme New', 'acme new', "
                "'acme.com', 'unconfirmed')"
            ),
            {"i": new_claimant, "t": tid},
        )
        await s.commit()

    try:
        async with await _client_for(tid, uid) as http:
            r = await http.post(f"/api/clients/{ids['live']}/unmerge")
        # Never an unhandled constraint error. This codebase refuses the
        # unmerge with a 409 naming the client that now holds the domain,
        # rather than silently dropping the domain from the resurrected row.
        assert r.status_code == 409
    finally:
        # The fixture teardown resets every remaining row in this tenant to
        # 'unconfirmed' in one blanket UPDATE; two unconfirmed rows both still
        # on acme.com (this test's extra new_claimant) would hit the same
        # index there. Clear the domain this test introduced so teardown's
        # own UPDATE doesn't reproduce the bug under test.
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE clients SET email_domain = NULL WHERE id = :i"),
                {"i": new_claimant},
            )
            await s.commit()


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
