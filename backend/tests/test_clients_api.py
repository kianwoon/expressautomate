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
from app.core.config import settings
from app.main import app
from app.services.client_naming import normalize_company_name
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


async def test_the_list_can_be_searched_by_name(agency_with_clients) -> None:
    """Type-to-search, for the client picker on the manual job order form.

    An agency accumulates hundreds of clients, so that picker cannot preload
    the list the way the 3-50 member picker does. A miss returns an empty
    page rather than the whole tenant — a picker that silently ignores the
    query would offer every client under a name that matches none of them.
    """
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        hit = (await http.get("/api/clients?q=cm")).json()
        miss = (await http.get("/api/clients?q=zzz")).json()
        # A LIKE metacharacter is matched as itself, not as a wildcard.
        literal = (await http.get("/api/clients?q=%25")).json()
    assert [row["id"] for row in hit["items"]] == [str(ids["live"])]
    assert miss["items"] == []
    assert literal["items"] == []


async def test_the_status_filter_is_the_review_queue(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?status=unconfirmed")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]
    assert body["counts"]["unconfirmed"] == 1


@pytest.fixture
async def agency_with_multi_referral_client():
    """One client referred by two distinct buddies.

    `buddy_referrals` is one-to-many from a client (one row per buddy), so a
    naive JOIN multiplies the client row once per referrer. This fixture seeds
    that exact shape so the dedup tests can prove the list and detail endpoints
    return one row, not two.
    """
    tid, uid = uuid.uuid4(), uuid.uuid4()
    cid = uuid.uuid4()
    buddy_a, buddy_b = uuid.uuid4(), uuid.uuid4()
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
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Acme', 'acme', 'unconfirmed')"
            ),
            {"i": cid, "t": tid},
        )
        await s.execute(
            text(
                "INSERT INTO buddies (id, tenant_id, name, email, source) "
                "VALUES (:i, :t, :n, :e, 'pipeline')"
            ),
            {"i": buddy_a, "t": tid, "n": "Recruiter A", "e": "a@buddy.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO buddies (id, tenant_id, name, email, source) "
                "VALUES (:i, :t, :n, :e, 'pipeline')"
            ),
            {"i": buddy_b, "t": tid, "n": "Recruiter B", "e": "b@buddy.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO buddy_referrals (id, tenant_id, buddy_id, client_id) "
                "VALUES (:i, :t, :b, :c)"
            ),
            {"i": uuid.uuid4(), "t": tid, "b": buddy_a, "c": cid},
        )
        await s.execute(
            text(
                "INSERT INTO buddy_referrals (id, tenant_id, buddy_id, client_id) "
                "VALUES (:i, :t, :b, :c)"
            ),
            {"i": uuid.uuid4(), "t": tid, "b": buddy_b, "c": cid},
        )
        await s.commit()
    yield tid, uid, cid
    await cleanup_tenant(tid)


async def test_a_client_with_multiple_referrals_lists_once(
    agency_with_multi_referral_client,
) -> None:
    """The buddy_referrals relation is one-to-many, so a JOIN would multiply
    client rows — a client referred by two buddies rendered twice, `total` was
    inflated, and pagination consumed two slots for one client. The correlated
    scalar subquery collapses to one buddy name per client, so one referrer or
    five, the row count is the client count."""
    tid, uid, cid = agency_with_multi_referral_client
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(cid)
    assert body["total"] == 1


async def test_a_client_with_multiple_referrals_loads_in_detail(
    agency_with_multi_referral_client,
) -> None:
    """The detail endpoint called `.one_or_none()` on a query that could return
    multiple rows (one per buddy), raising `MultipleResultsFound` (500) for any
    multi-referral client. The scalar subquery keeps it one row."""
    tid, uid, cid = agency_with_multi_referral_client
    async with await _client_for(tid, uid) as http:
        response = await http.get(f"/api/clients/{cid}")
    assert response.status_code == 200
    assert response.json()["id"] == str(cid)


@pytest.fixture
async def agency_with_named_clients():
    """Three live clients whose names, domains and statuses differ, so the
    letter bar and every sort column have something to disagree about.

    "Acme" is shared with `agency_with_clients`, but this fixture stands on its
    own: sorting and the A–Z index need more than one row to be meaningful,
    and the single-row fixture cannot tell ascending from descending.
    """
    tid, uid = uuid.uuid4(), uuid.uuid4()
    rows = [
        # name, name_normalized, email_domain, status — order is deliberately
        # not the order any sort returns, so the assertions are not tautological.
        ("Beacon Corp", "beacon corp", "beacon.sg", "confirmed", uuid.uuid4()),
        ("Acme", "acme", "acme.com", "unconfirmed", uuid.uuid4()),
        ("Zenith Ltd", "zenith ltd", None, "archived", uuid.uuid4()),
        # A second "B" so the within-letter order is observable: recency would
        # interleave other letters, alphabetical keeps the two B's together.
        ("Bora Trading", "bora trading", "bora.sg", "confirmed", uuid.uuid4()),
    ]
    ids = {name: cid for name, *_rest, cid in rows}
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
        for name, norm, domain, status, cid in rows:
            await s.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                    "email_domain, status) VALUES (:i, :t, :n, :norm, :d, :s)"
                ),
                {"i": cid, "t": tid, "n": name, "norm": norm, "d": domain, "s": status},
            )
        await s.commit()
    yield tid, uid, ids
    await cleanup_tenant(tid)


async def test_the_initial_filter_keeps_only_one_letter(
    agency_with_named_clients,
) -> None:
    tid, uid, ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?initial=B")).json()
    assert {row["name"] for row in body["items"]} == {"Beacon Corp", "Bora Trading"}


async def test_initials_lists_letters_with_rows_and_omits_empty_ones(
    agency_with_named_clients,
) -> None:
    """The bar answers "which letters could I click next", so it is computed
    before the `initial` filter narrows the page — and it never names a letter
    that would return an empty list."""
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
        # The bar is unchanged while standing on a letter, same as candidates.
        narrowed = (await http.get("/api/clients?initial=B")).json()
    assert body["initials"] == ["A", "B", "Z"]
    assert narrowed["initials"] == ["A", "B", "Z"]


async def test_selecting_a_letter_orders_alphabetically(
    agency_with_named_clients,
) -> None:
    """Recency inside a letter reads as no order at all — a recruiter scanning
    a letter is looking for a name, so the list meets them in reading order
    the way the candidates page does under the same control. Two clients share
    the letter B here precisely so that order is observable rather than trivial."""
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?initial=B")).json()
    assert [row["name"] for row in body["items"]] == ["Beacon Corp", "Bora Trading"]


async def test_sort_by_name_ascending(agency_with_named_clients) -> None:
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?sort_by=name")).json()
    assert [row["name"] for row in body["items"]] == [
        "Acme",
        "Beacon Corp",
        "Bora Trading",
        "Zenith Ltd",
    ]


async def test_sort_by_name_descending(agency_with_named_clients) -> None:
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?sort_by=name&descending=true")).json()
    assert [row["name"] for row in body["items"]] == [
        "Zenith Ltd",
        "Bora Trading",
        "Beacon Corp",
        "Acme",
    ]


async def test_sort_by_email_domain_puts_nulls_last(
    agency_with_named_clients,
) -> None:
    tid, uid, ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?sort_by=email_domain")).json()
    # Zenith has no domain; nulls last means it sits at the bottom.
    assert [row["id"] for row in body["items"]][-1] == str(ids["Zenith Ltd"])


async def test_sort_by_status(agency_with_named_clients) -> None:
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?sort_by=status")).json()
    # Ascending alphabetic over the status literals. Two confirmed clients tie
    # on status and fall back to id — assert only the status sequence, which is
    # the thing the sort key decides.
    assert [row["status"] for row in body["items"]] == [
        "archived",
        "confirmed",
        "confirmed",
        "unconfirmed",
    ]


async def test_an_unknown_sort_column_is_rejected(agency_with_named_clients) -> None:
    """The whitelist is the type system, not a lookup: a typo is a 422, never
    silently ignored as "no sort"."""
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        res = await http.get("/api/clients?sort_by=bogus")
    assert res.status_code == 422


async def test_the_default_order_is_by_recency_not_name(
    agency_with_clients,
) -> None:
    """No sort param means the review queue: what changed lately. This is the
    order the list had before sorting existed, and it must not move."""
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]


async def test_explicit_last_seen_sort_matches_the_default_order(
    agency_with_named_clients,
) -> None:
    """The frontend always sends `sort_by=last_seen&descending=true` so the
    table's active-column highlight is honest. That must agree with the no-sort
    default row-for-row, or the list would appear to re-order itself the moment
    the page loads. The `last_seen` sort carries `created_at` as its secondary
    key precisely to preserve the default's tiebreak."""
    tid, uid, _ids = agency_with_named_clients
    async with await _client_for(tid, uid) as http:
        default = (await http.get("/api/clients")).json()
        explicit = (await http.get("/api/clients?sort_by=last_seen&descending=true")).json()
    assert [row["id"] for row in default["items"]] == [
        row["id"] for row in explicit["items"]
    ]


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


async def test_suspend_requires_confirmed(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        response = await http.post(f"/api/clients/{ids['live']}/suspend", json={})
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"].lower()


async def test_suspend_then_unsuspend_returns_to_confirmed(agency_with_clients) -> None:
    """Unsuspend lands on `confirmed`, not `unconfirmed` — a suspension never
    revoked the judgement that the agency works with this firm."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{target}/confirm")).status_code == 200

        suspended = await http.post(
            f"/api/clients/{target}/suspend", json={"reason": "Invoice 4021 unpaid"}
        )
        assert suspended.status_code == 200
        assert suspended.json()["status"] == "suspended"

        detail = (await http.get(f"/api/clients/{target}")).json()
        assert detail["suspended_reason"] == "Invoice 4021 unpaid"
        assert detail["suspended_at"] is not None

        # Idempotent: a double-clicked button is not a mistake worth an error.
        assert (
            await http.post(f"/api/clients/{target}/suspend", json={})
        ).status_code == 200

        restored = await http.post(f"/api/clients/{target}/unsuspend")
        assert restored.status_code == 200
        assert restored.json()["status"] == "confirmed"

        detail = (await http.get(f"/api/clients/{target}")).json()
    assert detail["suspended_reason"] is None
    assert detail["suspended_at"] is None


async def test_unsuspend_refuses_a_live_client(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{target}/confirm")).status_code == 200
        response = await http.post(f"/api/clients/{target}/unsuspend")
    assert response.status_code == 400


async def test_archive_from_suspended_clears_the_suspension(agency_with_clients) -> None:
    """A hold that becomes permanent needs no unsuspend hop — but the reason
    must not outlive the state it described."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{target}/confirm")).status_code == 200
        await http.post(f"/api/clients/{target}/suspend", json={"reason": "Dispute"})

        archived = await http.post(f"/api/clients/{target}/archive")
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"

        detail = (await http.get(f"/api/clients/{target}")).json()
    assert detail["suspended_reason"] is None
    assert detail["suspended_at"] is None


async def test_merging_a_suspended_client_clears_its_suspension(agency_with_clients) -> None:
    """A stale reason can never outlive the state it describes — merge is
    another exit from `suspended`, same as archive."""
    tid, uid, ids = agency_with_clients
    loser = ids["live"]
    target = ids["merged"]
    async with AdminSessionLocal() as s:
        # Free up "merged" to act as the live merge target.
        await s.execute(
            text(
                "UPDATE clients SET status = 'unconfirmed', merged_into_client_id = NULL, "
                "email_domain = NULL WHERE id = :i"
            ),
            {"i": target},
        )
        await s.commit()

    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{loser}/confirm")).status_code == 200
        assert (
            await http.post(f"/api/clients/{loser}/suspend", json={"reason": "Dispute"})
        ).status_code == 200

        merged = await http.post(
            f"/api/clients/{loser}/merge", json={"target_id": str(target)}
        )
        assert merged.status_code == 200

        detail = (await http.get(f"/api/clients/{loser}")).json()
        assert detail["suspended_reason"] is None
        assert detail["suspended_at"] is None

        restored = await http.post(f"/api/clients/{loser}/unmerge")
        assert restored.status_code == 200

        detail = (await http.get(f"/api/clients/{loser}")).json()
    assert detail["status"] == "unconfirmed"
    assert detail["suspended_reason"] is None
    assert detail["suspended_at"] is None


async def test_confirm_on_suspended_names_unsuspend(agency_with_clients) -> None:
    """Not "restore it before marking it confirmed" — that names an endpoint
    which would refuse this row."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{target}/confirm")).status_code == 200
        await http.post(f"/api/clients/{target}/suspend", json={})

        response = await http.post(f"/api/clients/{target}/confirm")
    assert response.status_code == 400
    assert "unsuspend" in response.json()["detail"].lower()


async def test_suspended_is_a_live_client_in_the_listing(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{target}/confirm")).status_code == 200
        await http.post(f"/api/clients/{target}/suspend", json={})

        listing = (await http.get("/api/clients")).json()
        assert str(target) in [row["id"] for row in listing["items"]]
        assert listing["counts"]["suspended"] == 1
        assert listing["counts"]["all"] >= 1

        filtered = (await http.get("/api/clients?status=suspended")).json()
    assert [row["id"] for row in filtered["items"]] == [str(target)]


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


async def test_create_client_starts_confirmed_and_manual(agency_with_clients) -> None:
    """A recruiter typing the name IS the human judgement `confirmed` records.
    Sending it to review would ask them to confirm what they just asserted."""
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        response = await http.post(
            "/api/clients",
            json={
                "name": "Meridian Partners  Pte Ltd",
                "email_domain": "MERIDIAN.com.sg ",
                "fee_percent": 18.5,
                "payment_terms_days": 30,
                "notes": "Introduced by Lim",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["source"] == "manual"
    # Lowercased and stripped, so it can never miss a match on whitespace.
    assert body["email_domain"] == "meridian.com.sg"
    assert body["fee_percent"] == 18.5
    assert body["name_normalized"] == normalize_company_name("Meridian Partners  Pte Ltd")


async def test_create_client_without_a_domain(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        response = await http.post("/api/clients", json={"name": "Referral Only Ltd"})
    assert response.status_code == 201
    assert response.json()["email_domain"] is None


async def test_create_client_refuses_a_free_provider_domain(agency_with_clients) -> None:
    """`gmail.com` identifies a person, not a company. Storing it would claim
    the tenant's one slot for it and match every Gmail sender to this client."""
    tid, uid, ids = agency_with_clients
    free = next(iter(settings.FREE_EMAIL_DOMAINS))
    async with await _client_for(tid, uid) as http:
        response = await http.post(
            "/api/clients", json={"name": "Sole Trader", "email_domain": free}
        )
    assert response.status_code == 422


async def test_create_client_names_the_domain_holder(agency_with_clients) -> None:
    """409, never a silent adoption: "Add client" must not sometimes mean
    "edit a row you did not know existed"."""
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        existing = (await http.get(f"/api/clients/{ids['live']}")).json()
        assert existing["email_domain"] is not None

        response = await http.post(
            "/api/clients",
            json={"name": "Same Firm Retyped", "email_domain": existing["email_domain"]},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert existing["name"] in detail
    assert existing["status"] in detail


async def test_patch_renames_and_renormalises(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        response = await http.patch(
            f"/api/clients/{target}", json={"name": "Acme Holdings Pte Ltd"}
        )
    assert response.status_code == 200
    assert response.json()["name"] == "Acme Holdings Pte Ltd"
    assert response.json()["name_normalized"] == normalize_company_name("Acme Holdings Pte Ltd")


async def test_patch_can_clear_the_domain(agency_with_clients) -> None:
    """A legitimate edit — "we got this wrong" — leaving the row on name-only
    matching, where every free-provider-sender row already sits."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        response = await http.patch(f"/api/clients/{target}", json={"email_domain": None})
    assert response.status_code == 200
    assert response.json()["email_domain"] is None


async def test_patch_into_a_taken_domain_is_409(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        holder = (await http.get(f"/api/clients/{ids['live']}")).json()
        created = (await http.post("/api/clients", json={"name": "Other Firm"})).json()

        response = await http.patch(
            f"/api/clients/{created['id']}", json={"email_domain": holder["email_domain"]}
        )
    assert response.status_code == 409
    assert holder["name"] in response.json()["detail"]


async def test_patch_refuses_a_merged_client(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        response = await http.patch(f"/api/clients/{ids['merged']}", json={"name": "Nope"})
    assert response.status_code == 400
    assert "unmerge" in response.json()["detail"].lower()


async def test_patch_only_touches_the_field_sent(agency_with_clients) -> None:
    """`exclude_unset=True` is what makes a single-field PATCH mean "change
    this one thing", not "reset every omitted column to null". Nothing else
    here would fail if that flag were dropped."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        response = await http.patch(f"/api/clients/{target}", json={"notes": "Called back"})
    assert response.status_code == 200
    body = response.json()
    assert body["notes"] == "Called back"
    assert body["email_domain"] == "acme.com"


async def test_patch_cannot_write_status_or_source(agency_with_clients) -> None:
    """status and source are not fields on the patch body — Pydantic drops
    them silently. This only proves it, so a future `extra = "allow"` would
    fail it rather than sail through unnoticed."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        response = await http.patch(
            f"/api/clients/{target}",
            json={"status": "archived", "source": "manual", "notes": "Follow up"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unconfirmed"
    assert body["source"] == "pipeline"
    assert body["notes"] == "Follow up"


@pytest.fixture
async def other_agency_client_id():
    """A client under a second tenant — for asserting cross-tenant 404s."""
    other_tid, other_uid = uuid.uuid4(), uuid.uuid4()
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": other_tid, "n": f"other-{other_tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": other_uid, "t": other_tid, "e": f"o{other_uid.hex[:6]}@other.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Other Co', 'other co', 'confirmed')"
            ),
            {"i": cid, "t": other_tid},
        )
        await s.commit()
    yield cid
    await cleanup_tenant(other_tid)


async def test_contacts_keep_exactly_one_primary(agency_with_clients) -> None:
    """Two contacts posted as primary in sequence: the second demotes the
    first, because `uq_client_contacts_one_primary` permits nothing else."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        first = await http.post(
            f"/api/clients/{target}/contacts",
            json={"name": "Priya Menon", "title": "Head of Talent", "is_primary": True},
        )
        assert first.status_code == 201

        second = await http.post(
            f"/api/clients/{target}/contacts",
            json={"name": "Daniel Ong", "email": "daniel@example.com", "is_primary": True},
        )
        assert second.status_code == 201

        contacts = (await http.get(f"/api/clients/{target}")).json()["contacts"]
    assert len(contacts) == 2
    assert [c["name"] for c in contacts if c["is_primary"]] == ["Daniel Ong"]


async def test_contact_patch_and_delete(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        created = (
            await http.post(f"/api/clients/{target}/contacts", json={"name": "Temp"})
        ).json()

        patched = await http.patch(
            f"/api/clients/{target}/contacts/{created['id']}",
            json={"name": "Temporary Contact", "phone": "+6591234567"},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Temporary Contact"

        deleted = await http.delete(f"/api/clients/{target}/contacts/{created['id']}")
        assert deleted.status_code == 204
        assert (await http.get(f"/api/clients/{target}")).json()["contacts"] == []


async def test_contacts_of_another_agency_are_404(
    agency_with_clients, other_agency_client_id
) -> None:
    """Read, patch and delete alike. A 403 would itself disclose that the id
    exists — the same reasoning as `_load`."""
    tid, uid, _ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (
            await http.get(f"/api/clients/{other_agency_client_id}")
        ).status_code == 404
        assert (
            await http.post(
                f"/api/clients/{other_agency_client_id}/contacts", json={"name": "X"}
            )
        ).status_code == 404

        contact_id = uuid.uuid4()
        assert (
            await http.patch(
                f"/api/clients/{other_agency_client_id}/contacts/{contact_id}",
                json={"name": "X"},
            )
        ).status_code == 404
        assert (
            await http.delete(
                f"/api/clients/{other_agency_client_id}/contacts/{contact_id}"
            )
        ).status_code == 404


async def test_contact_patch_with_explicit_null_name_is_rejected(agency_with_clients) -> None:
    """`name` is NOT NULL. An explicit `{"name": null}` must 422 naming the
    field, not reach the UPDATE and 500 — the same rule `_name_is_not_blank`
    already enforces for a client's own name."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with await _client_for(tid, uid) as http:
        created = (
            await http.post(f"/api/clients/{target}/contacts", json={"name": "Temp"})
        ).json()

        response = await http.patch(
            f"/api/clients/{target}/contacts/{created['id']}",
            json={"name": None},
        )
        assert response.status_code == 422

        blank = await http.patch(
            f"/api/clients/{target}/contacts/{created['id']}",
            json={"name": "   "},
        )
        assert blank.status_code == 422


async def test_a_contact_is_not_reachable_through_another_clients_url(
    agency_with_clients,
) -> None:
    """`_load_contact`'s own `client_id` filter — not just the cross-tenant
    check `_load` provides — must stop client A's contact being reached
    through client B's URL, even within the same tenant."""
    tid, uid, ids = agency_with_clients
    client_a = ids["live"]
    client_b = ids["merged"]
    async with await _client_for(tid, uid) as http:
        created = (
            await http.post(f"/api/clients/{client_a}/contacts", json={"name": "A Contact"})
        ).json()
        contact_id = created["id"]

        assert (
            await http.patch(
                f"/api/clients/{client_b}/contacts/{contact_id}", json={"name": "Hijacked"}
            )
        ).status_code == 404
        assert (
            await http.delete(f"/api/clients/{client_b}/contacts/{contact_id}")
        ).status_code == 404
