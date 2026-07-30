"""The list is a review queue before it is a directory.

Most rows in a young tenant are unconfirmed proposals, so the default view
excludes merged rows (which are no longer anyone's client) and the counts are
computed over the whole tenant rather than the page — a chip that shrank as
you paged would answer a different question than it appears to.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import clients as clients_api
from app.main import app
from tests.conftest import AdminSessionLocal, cleanup_tenant

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
    await cleanup_tenant(tid)


async def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_list_hides_merged_rows_by_default(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]


async def test_the_all_count_excludes_merged_rows_like_the_default_list(
    agency_with_clients,
) -> None:
    """The chip cannot promise more rows than the unfiltered list can show.

    The fixture has one live client and one merged one. "all" must equal the
    length of the default (merged-excluding) listing, not the whole-tenant
    row count — otherwise the chip shows 2 while the list under it shows 1.
    """
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
    assert body["counts"]["all"] == len(body["items"])
    assert body["counts"]["all"] == 1
    # The merged row is still counted under its own status and reachable
    # via the explicit filter.
    assert body["counts"]["merged"] == 1
    async with await _client_for(tid, uid) as http:
        merged_body = (await http.get("/api/clients?status=merged")).json()
    assert [row["id"] for row in merged_body["items"]] == [str(ids["merged"])]


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


async def test_archive_then_restore_returns_to_unconfirmed(agency_with_clients) -> None:
    """Archiving a client must be reversible, the same as archiving a candidate."""
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        r = await http.post(f"/api/clients/{ids['live']}/archive")
        assert r.status_code == 200
        body = (await http.get(f"/api/clients/{ids['live']}")).json()
        assert body["status"] == "archived"

        r = await http.post(f"/api/clients/{ids['live']}/restore")
        assert r.status_code == 200
        assert r.json()["status"] == "unconfirmed"
        body = (await http.get(f"/api/clients/{ids['live']}")).json()
        assert body["status"] == "unconfirmed"


async def test_restoring_a_merged_client_is_refused(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        r = await http.post(f"/api/clients/{ids['merged']}/restore")
    assert r.status_code == 400


async def test_restoring_a_confirmed_client_is_refused(agency_with_clients) -> None:
    """The defect this guards against: restore must not demote a confirmed

    client back to unconfirmed. A human's confirmation is a judgement, and an
    endpoint whose name says "restore" must not be able to erase it.
    """
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['live']}/confirm")).status_code == 200
        r = await http.post(f"/api/clients/{ids['live']}/restore")
        assert r.status_code == 400
        assert "confirmed" in r.json()["detail"]
        body = (await http.get(f"/api/clients/{ids['live']}")).json()
    assert body["status"] == "confirmed"


async def test_restoring_an_unconfirmed_client_is_refused(agency_with_clients) -> None:
    """Nothing to restore: the row was never archived."""
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        r = await http.post(f"/api/clients/{ids['live']}/restore")
    assert r.status_code == 400


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


async def test_merge_keeps_both_null_message_mentions_after_a_collision(
    agency_with_clients,
) -> None:
    """A NULL message id is not a duplicate of another NULL message id.

    The unique constraint declares NULLS NOT DISTINCT, so two NULL-message
    mentions on the *same* client collide with each other — but a NULL id
    means the source email was retention-purged, and two purged mentions are
    evidence of two different purged emails, not one email counted twice.
    Neither may be deleted. Since the constraint forbids moving the loser's
    NULL mention onto a target that already has one, it must stay put on the
    loser row, which survives the merge (status becomes `merged`, reachable
    by id) rather than being deleted.
    """
    tid, uid, ids = agency_with_clients
    async with AdminSessionLocal() as s:
        # ids['live'] (the merge target) already has a mention with no message.
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, "
                "matched_by) VALUES (:i, :t, :c, NULL, 'name')"
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
    # Both no-message mentions survive: one on each row. No evidence is lost,
    # even though the stronger (email_domain) one stayed on the now-merged
    # loser rather than moving to the surviving client.
    assert sorted(row[0] for row in rows) == sorted([ids["live"], ids["merged"]])


async def test_merge_keeps_the_stronger_mention_on_a_real_message_collision(
    agency_with_clients,
) -> None:
    """On a genuine same-message collision, the better evidence must win.

    Both clients hold a mention for the same real message id. The loser's
    mention is the stronger claim (email_domain) against the target's weaker
    one (name) — matched_by alone ranks them, since there is no confidence
    column to fall back on. The merge must not simply keep whichever
    happened to already be on the target.
    """
    tid, uid, ids = agency_with_clients
    message_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        # A real mailboxes row is required for the email_messages FK, and a
        # real email_messages row is required for the client_mentions FK.
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, ms_user_id, scope, folder_id, "
                "retention_months) VALUES (:i, :t, :m, 'inbox', 'inbox', 12)"
            ),
            {"i": mailbox_id, "t": tid, "m": f"ms-{mailbox_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id) "
                "VALUES (:i, :t, :m, :g)"
            ),
            {"i": message_id, "t": tid, "m": mailbox_id, "g": f"g{message_id.hex[:8]}"},
        )
        # Target holds the weaker mention for this message.
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, "
                "matched_by) VALUES (:i, :t, :c, :m, 'name')"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": ids["live"], "m": message_id},
        )
        await s.execute(
            text(
                "UPDATE clients SET status = 'unconfirmed', merged_into_client_id = NULL "
                "WHERE id = :i"
            ),
            {"i": ids["merged"]},
        )
        # Loser holds the stronger mention for the same message.
        loser_mention_id = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, "
                "matched_by) VALUES (:i, :t, :c, :m, 'email_domain')"
            ),
            {"i": loser_mention_id, "t": tid, "c": ids["merged"], "m": message_id},
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
                    "SELECT id, client_id, matched_by FROM client_mentions "
                    "WHERE tenant_id = :t AND email_message_id = :m"
                ),
                {"t": tid, "m": message_id},
            )
        ).fetchall()
    # Exactly one mention for this message survives: the stronger one, now on
    # the surviving client — not the target's original weaker row.
    assert len(rows) == 1
    assert rows[0][0] == loser_mention_id
    assert rows[0][1] == ids["live"]
    assert rows[0][2] == "email_domain"


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
    tid, uid, ids = agency_with_clients
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
            r = await http.post(f"/api/clients/{ids['live']}/restore")
            assert r.status_code == 404
    finally:
        await cleanup_tenant(other_tid)


# --- The bugs an adversarial pass through the real API turned up. ------------


async def _seed_client(tid: uuid.UUID, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, :n, :n, 'unconfirmed')"
            ),
            {"i": cid, "t": tid, "n": name},
        )
        await s.commit()
    return cid


# Named to match `_RACE_PAUSE_SECONDS` in test_candidates_api.py — see the
# note there for why the pause is fixed rather than hoped for.
_RACE_PAUSE_SECONDS = 0.15


async def test_two_opposing_merges_at_once_cannot_make_a_cycle(
    agency_with_clients, monkeypatch
) -> None:
    """The candidate endpoint had this bug observably; this one is a near-copy

    of it, so it had it too. A→B and B→A raced left both rows pointing at
    each other: neither in any list, neither unmergeable back into a live row.

    The fixed pause in `_load` is what makes the race deterministic rather than
    hoped for — see the note in `test_candidates_api.py`.
    """
    tid, uid, _ = agency_with_clients
    a = await _seed_client(tid, "race a")
    b = await _seed_client(tid, "race b")

    real_load = clients_api._load

    async def paced_load(session, client_id):
        row = await real_load(session, client_id)
        await asyncio.sleep(_RACE_PAUSE_SECONDS)
        return row

    monkeypatch.setattr(clients_api, "_load", paced_load)

    async with await _client_for(tid, uid) as one, await _client_for(tid, uid) as two:
        first, second = await asyncio.gather(
            one.post(f"/api/clients/{a}/merge", json={"target_id": str(b)}),
            two.post(f"/api/clients/{b}/merge", json={"target_id": str(a)}),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 400], (
        first.text,
        second.text,
    )
    async with AdminSessionLocal() as s:
        pointers = (
            await s.execute(
                text("SELECT merged_into_client_id FROM clients WHERE id IN (:a, :b)"),
                {"a": a, "b": b},
            )
        ).scalars().all()
    assert len([p for p in pointers if p is not None]) == 1


async def test_an_archived_client_must_be_restored_before_it_can_be_confirmed(
    agency_with_clients,
) -> None:
    """`restore` exists so an archived client re-enters the review queue rather

    than silently becoming live again. Confirming straight out of `archived`
    walked past it.
    """
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['live']}/archive")).status_code == 200
        refused = await http.post(f"/api/clients/{ids['live']}/confirm")
        assert refused.status_code == 400
        assert (await http.get(f"/api/clients/{ids['live']}")).json()["status"] == "archived"

        assert (await http.post(f"/api/clients/{ids['live']}/restore")).status_code == 200
        allowed = await http.post(f"/api/clients/{ids['live']}/confirm")
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "confirmed"


async def test_confirming_and_archiving_stay_idempotent(agency_with_clients) -> None:
    """A double-clicked button is not a mistake worth an error."""
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['live']}/confirm")).status_code == 200
        assert (await http.post(f"/api/clients/{ids['live']}/confirm")).status_code == 200
        assert (await http.post(f"/api/clients/{ids['live']}/archive")).status_code == 200
        assert (await http.post(f"/api/clients/{ids['live']}/archive")).status_code == 200


async def test_client_contacts_is_tenant_isolated(agency_with_clients) -> None:
    """The new table's RLS policy, exercised through the runtime role.

    `verify_rls_enforced()` only checks the table has FORCE RLS; this checks
    the policy predicate actually filters.
    """
    from sqlalchemy import select

    from app.db.rls import tenant_session
    from app.models.client import ClientContact

    tid, uid, ids = agency_with_clients
    async with tenant_session(tid) as session:
        session.add(
            ClientContact(
                tenant_id=tid,
                client_id=ids["live"],
                name="Priya",
                is_primary=True,
            )
        )
        await session.commit()

    other_tenant = uuid.uuid4()
    async with tenant_session(other_tenant) as session:
        rows = (await session.execute(select(ClientContact))).scalars().all()
    assert rows == []
