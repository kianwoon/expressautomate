"""The buddy network list — search, A–Z index, sort and the referral modal.

A buddy is an external recruiter who forwards job orders into the user's
mailbox. The list is small (the partner-agency colleagues who forward work),
so it carries no pagination — but it carries the same letter bar, search and
column sort as the clients and candidates lists, so a recruiter who has
learned those controls on one page finds them here too.

"Referrals" counts the *work* a buddy has sent — job orders, not buddy→client
links — so the number on a row and the list in the modal behind it agree.
"""

import uuid
from datetime import UTC, datetime, timedelta

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
    sort column, the A–Z bar and the period filter have something to disagree
    about.

    Each buddy refers one client, and the *client* carries a controlled number
    of job orders (opportunities) at known timestamps. The referral count a
    recruiter sees is the number of those job orders — the work, not the link —
    so the fixture puts opportunities on the client rather than piling up
    buddy_referrals rows. A few job orders are dated far in the past so the
    period filter has rows to drop.
    """
    tid, uid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)

    # name, email, domain, [(days_ago, title), ...] — the job orders on the
    # buddy's referred client. days_ago=40 lands outside the 30d window.
    spec = [
        ("Beacon", "beacon@beacon.sg", "beacon.sg", [(0, "Beacon role")]),
        (
            "Acme", "acme@acme.com", "acme.com",
            [(0, "Acme role A"), (5, "Acme role B"), (40, "Acme role C")],
        ),
        ("Zenith", "zenith@other.io", None, []),
        ("Bora", "bora@bora.sg", "bora.sg", [(1, "Bora role A"), (40, "Bora role B")]),
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
        for name, email, domain, jobs in spec:
            bid = ids[name]
            await s.execute(
                text(
                    "INSERT INTO buddies (id, tenant_id, name, email, email_domain) "
                    "VALUES (:i, :t, :n, :e, :d)"
                ),
                {"i": bid, "t": tid, "n": name, "e": email, "d": domain},
            )
            # One referred client per buddy. A buddy who forwards five job
            # orders for one client is one referral link and five job orders —
            # and the count is the five.
            cid = uuid.uuid4()
            await s.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                    "VALUES (:i, :t, :n, :n, 'unconfirmed')"
                ),
                {"i": cid, "t": tid, "n": f"client-{name}"},
            )
            rid = uuid.uuid4()
            await s.execute(
                text(
                    "INSERT INTO buddy_referrals (id, tenant_id, buddy_id, client_id) "
                    "VALUES (:i, :t, :b, :c)"
                ),
                {"i": rid, "t": tid, "b": bid, "c": cid},
            )
            for days_ago, title in jobs:
                oid = uuid.uuid4()
                when = now - timedelta(days=days_ago)
                await s.execute(
                    text(
                        "INSERT INTO opportunities "
                        "(id, tenant_id, client_id, job_title_raw, company_name_raw, "
                        "received_datetime, source, review_status, quality_state) "
                        "VALUES (:i, :t, :c, :jt, :co, :w, 'pipeline', 'ready', 'likely')"
                    ),
                    {"i": oid, "t": tid, "c": cid, "jt": title, "co": name, "w": when},
                )
        await s.commit()
    yield tid, uid, ids
    await cleanup_tenant(tid)


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_default_order_is_referral_count_then_name(
    agency_with_buddies,
) -> None:
    """No sort param means the buddies who send the most work sit at the top,
    and name breaks the tie. All-time counts: Acme 3, Bora 2, Beacon 1, Zenith 0."""
    tid, uid, _ids = agency_with_buddies
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


async def test_referral_count_counts_job_orders_not_client_links(
    agency_with_buddies,
) -> None:
    """The count is the work, not the link: each buddy has exactly one referred
    client, but Acme's client carries three job orders and so Acme counts three."""
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies")).json()
    counts = {row["name"]: row["referral_count"] for row in body["items"]}
    assert counts == {"Acme": 3, "Bora": 2, "Beacon": 1, "Zenith": 0}


async def test_the_period_filter_scopes_the_count(agency_with_buddies) -> None:
    """Within 30 days the two 40-day-old job orders drop out: Acme 3→2, Bora 2→1.
    Zenith stays 0, Beacon stays 1."""
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?period=30d")).json()
    counts = {row["name"]: row["referral_count"] for row in body["items"]}
    assert counts == {"Acme": 2, "Bora": 1, "Beacon": 1, "Zenith": 0}


async def test_the_seven_day_window_counts_only_recent_work(
    agency_with_buddies,
) -> None:
    """7d keeps everything within a week and drops the 40-day-old rows. Acme's
    0- and 5-day-old job orders both survive (2); Bora keeps its 1-day one (1)."""
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies?period=7d")).json()
    counts = {row["name"]: row["referral_count"] for row in body["items"]}
    assert counts == {"Acme": 2, "Bora": 1, "Beacon": 1, "Zenith": 0}


async def test_a_buddy_with_no_job_orders_still_appears(agency_with_buddies) -> None:
    """Zenith referred a client but that client has no opportunities. The LEFT
    JOIN must keep the buddy at count 0, not drop the row."""
    tid, uid, ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/buddies")).json()
    zenith = next(r for r in body["items"] if r["id"] == str(ids["Zenith"]))
    assert zenith["referral_count"] == 0


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


# ---------------------------------------------------------------------------
# Referral modal — GET /buddies/{id}/referrals
# ---------------------------------------------------------------------------

async def test_the_referrals_endpoint_lists_job_orders(agency_with_buddies) -> None:
    """The modal behind the count shows the job orders behind the number. Acme
    has three, most recent first."""
    tid, uid, ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get(f"/api/buddies/{ids['Acme']}/referrals")).json()
    assert body["buddy"]["name"] == "Acme"
    assert body["total"] == 3
    titles = [row["job_title_raw"] for row in body["items"]]
    # Most recent first: 0-day, then 5-day, then 40-day.
    assert titles == ["Acme role A", "Acme role B", "Acme role C"]
    # Each row carries the compact fields the modal renders.
    first = body["items"][0]
    assert set(first.keys()) == {
        "id", "job_title_raw", "company_name_raw", "received_datetime",
        "location_raw", "salary_raw",
    }


async def test_the_referrals_endpoint_scopes_to_period(agency_with_buddies) -> None:
    """The same period that scopes the count scopes the modal list, so the two
    agree: Acme at 30d drops the 40-day-old job order to 2."""
    tid, uid, ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get(f"/api/buddies/{ids['Acme']}/referrals?period=30d")).json()
    assert body["total"] == 2
    assert {row["job_title_raw"] for row in body["items"]} == {"Acme role A", "Acme role B"}


async def test_the_referrals_endpoint_for_a_buddy_with_no_work_is_empty(
    agency_with_buddies,
) -> None:
    """Zenith has a referred client but no job orders — the modal opens empty,
    not as an error."""
    tid, uid, ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        body = (await http.get(f"/api/buddies/{ids['Zenith']}/referrals")).json()
    assert body["total"] == 0
    assert body["items"] == []


async def test_the_referrals_endpoint_404s_for_an_unknown_buddy(
    agency_with_buddies,
) -> None:
    tid, uid, _ids = agency_with_buddies
    async with await _client_for(tid, uid) as http:
        res = await http.get(f"/api/buddies/{uuid.uuid4()}/referrals")
    assert res.status_code == 404
