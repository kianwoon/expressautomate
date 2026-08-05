"""The buddy network list — search, A–Z index and sort.

A buddy is an external recruiter who forwards job orders into the user's
mailbox. The list is small (the partner-agency colleagues who forward work),
so it carries no pagination — but it carries the same letter bar, search and
column sort as the clients and candidates lists, so a recruiter who has
learned those controls on one page finds them here too.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal, cleanup_tenant

# Reuse, do not redefine: this is the same session cookie the real app reads.
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def agency_with_buddies():
    """Four buddies whose names, domains and referral counts differ, so every
    sort column and the A–Z bar have something to disagree about.

    Referral counts are set by inserting real `buddy_referrals` rows — the
    count is an aggregate the endpoint computes, not a column, so the only
    honest way to give a buddy three referrals is to refer three clients.
    """
    tid, uid = uuid.uuid4(), uuid.uuid4()
    # name, email, domain, #referrals
    spec = [
        ("Beacon", "beacon@beacon.sg", "beacon.sg", 1),
        ("Acme", "acme@acme.com", "acme.com", 3),
        ("Zenith", "zenith@other.io", None, 0),
        ("Bora", "bora@bora.sg", "bora.sg", 2),
    ]
    ids = {name: uuid.uuid4() for name, *_ in spec}
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
        for name, email, domain, n in spec:
            bid = ids[name]
            await s.execute(
                text(
                    "INSERT INTO buddies (id, tenant_id, name, email, email_domain) "
                    "VALUES (:i, :t, :n, :e, :d)"
                ),
                {"i": bid, "t": tid, "n": name, "e": email, "d": domain},
            )
            # Each referral needs a client row to point at. The client's own
            # fields are irrelevant to the buddy count — only the row's
            # existence is — so a minimal stub is enough.
            for _ in range(n):
                cid = uuid.uuid4()
                await s.execute(
                    text(
                        "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                        "VALUES (:i, :t, :n, :n, 'unconfirmed')"
                    ),
                    {"i": cid, "t": tid, "n": f"client-{cid.hex[:6]}"},
                )
                rid = uuid.uuid4()
                await s.execute(
                    text(
                        "INSERT INTO buddy_referrals (id, tenant_id, buddy_id, client_id) "
                        "VALUES (:i, :t, :b, :c)"
                    ),
                    {"i": rid, "t": tid, "b": bid, "c": cid},
                )
        await s.commit()
    yield tid, uid, ids
    await cleanup_tenant(tid)


async def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_default_order_is_referral_count_then_name(
    agency_with_buddies,
) -> None:
    """No sort param means the buddies who send the most work sit at the top,
    and name breaks the tie. This is the order the list had before sorting
    existed, and it must not move."""
    tid, uid, ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies")).json()
    assert [row["name"] for row in body["items"]] == ["Acme", "Bora", "Beacon", "Zenith"]


async def test_the_response_is_a_dict_not_a_bare_list(agency_with_buddies) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies")).json()
    assert body["total"] == 4
    assert isinstance(body["items"], list)
    assert isinstance(body["initials"], list)


async def test_search_matches_name_or_email(agency_with_buddies) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        by_name = (await http.get("/api/buddies?q=acm")).json()
        by_email = (await http.get("/api/buddies?q=beacon.sg")).json()
        miss = (await http.get("/api/buddies?q=zzz")).json()
        # A LIKE metacharacter is matched as itself, not as a wildcard.
        literal = (await http.get("/api/buddies?q=%25")).json()
    assert [row["name"] for row in by_name["items"]] == ["Acme"]
    assert [row["name"] for row in by_email["items"]] == ["Beacon"]
    assert miss["items"] == []
    assert literal["items"] == []


async def test_the_initial_filter_keeps_only_one_letter(agency_with_buddies) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?initial=B")).json()
    assert {row["name"] for row in body["items"]} == {"Beacon", "Bora"}


async def test_initials_lists_letters_with_rows_and_omits_empty_ones(
    agency_with_buddies,
) -> None:
    """The bar answers "which letters could I click next", so it is computed
    before the `initial` filter narrows the page — and never names a letter
    that would return an empty list."""
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies")).json()
        narrowed = (await http.get("/api/buddies?initial=B")).json()
    assert body["initials"] == ["A", "B", "Z"]
    assert narrowed["initials"] == ["A", "B", "Z"]


async def test_selecting_a_letter_orders_alphabetically(agency_with_buddies) -> None:
    """Recency or referral count inside a letter reads as no order at all — a
    recruiter scanning a letter is looking for a name, so the list meets them
    in reading order the way the clients page does under the same control."""
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?initial=B")).json()
    assert [row["name"] for row in body["items"]] == ["Beacon", "Bora"]


async def test_sort_by_name_ascending(agency_with_buddies) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?sort_by=name")).json()
    assert [row["name"] for row in body["items"]] == ["Acme", "Beacon", "Bora", "Zenith"]


async def test_sort_by_name_descending(agency_with_buddies) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?sort_by=name&descending=true")).json()
    assert [row["name"] for row in body["items"]] == ["Zenith", "Bora", "Beacon", "Acme"]


async def test_sort_by_referral_count_ascending(agency_with_buddies) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?sort_by=referral_count")).json()
    # Ascending: zero referrals first.
    assert [row["name"] for row in body["items"]] == ["Zenith", "Beacon", "Bora", "Acme"]


async def test_sort_by_email_domain_puts_nulls_last(agency_with_buddies) -> None:
    tid, uid, ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?sort_by=email_domain")).json()
    # Zenith has no domain; nulls last means it sits at the bottom.
    assert [row["id"] for row in body["items"]][-1] == str(ids["Zenith"])


async def test_an_unknown_sort_column_is_rejected(agency_with_buddies) -> None:
    """The whitelist is the type system, not a lookup: a typo is a 422, never
    silently ignored as "no sort"."""
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        res = await http.get("/api/buddies?sort_by=bogus")
    assert res.status_code == 422
