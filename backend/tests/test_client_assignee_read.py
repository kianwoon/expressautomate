"""Reading back who looks after a client.

The write side shipped first, which left the interface able to record an
assignment and unable to see it: every client rendered as unassigned, so the
reassignment control was offered to everyone and the server's 403 fired on an
action the reader had been invited to take. These tests pin the read side —
the id, the resolved name, and the two ways the resolution can go wrong: an
inner join that drops unassigned clients, and a name order that ignores
`preferred_name`.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


async def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def _user(
    tid: uuid.UUID,
    email: str,
    preferred_name: str | None = None,
    display_name: str | None = None,
) -> uuid.UUID:
    uid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role, preferred_name, "
                "display_name) VALUES (:i, :t, :e, 'recruiter', :p, :d)"
            ),
            {"i": uid, "t": tid, "e": email, "p": preferred_name, "d": display_name},
        )
        await s.commit()
    return uid


async def _client(tid: uuid.UUID, name: str, assigned: uuid.UUID | None) -> uuid.UUID:
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status, "
                "assigned_user_id) VALUES (:i, :t, :n, :z, 'confirmed', :a)"
            ),
            {"i": cid, "t": tid, "n": name, "z": name.lower(), "a": assigned},
        )
        await s.commit()
    return cid


@pytest.fixture
async def agency():
    """One agency; the caller, a named holder, and two clients.

    The holder carries both a `display_name` and a `preferred_name` so the
    order between them is testable rather than merely present.
    """
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:8]}"},
        )
        await s.commit()
    caller = await _user(tid, f"c{tid.hex[:8]}@agency.sg")
    holder = await _user(
        tid,
        f"priya.nair{tid.hex[:6]}@agency.sg",
        preferred_name="Priya",
        display_name="Priya Nair",
    )
    held = await _client(tid, "Acme", holder)
    free = await _client(tid, "Bolt", None)
    yield tid, caller, holder, held, free
    await cleanup_tenant(tid)


async def test_list_names_the_assignee(agency):
    tid, caller, holder, held, _free = agency
    async with await _http(tid, caller) as c:
        body = (await c.get("/api/clients")).json()

    row = next(item for item in body["items"] if item["id"] == str(held))
    assert row["assigned_user_id"] == str(holder)
    # `preferred_name`, not `display_name`: `app/models/tenant.py` says it takes
    # priority everywhere a name is shown, and this row shows one.
    assert row["assignee_name"] == "Priya"


async def test_detail_names_the_assignee(agency):
    tid, caller, holder, held, _free = agency
    async with await _http(tid, caller) as c:
        body = (await c.get(f"/api/clients/{held}")).json()

    assert body["assigned_user_id"] == str(holder)
    assert body["assignee_name"] == "Priya"


async def test_the_join_keeps_unassigned_clients(agency):
    """The whole reason the join is LEFT.

    An inner join would drop every client nobody holds — most of a fresh list —
    while the count chips (which do not join) went on counting them, so the
    page would say two and show one.
    """
    tid, caller, _holder, held, free = agency
    async with await _http(tid, caller) as c:
        body = (await c.get("/api/clients")).json()

    ids = {item["id"] for item in body["items"]}
    assert ids == {str(held), str(free)}
    assert body["total"] == 2
    assert body["counts"]["all"] == 2

    row = next(item for item in body["items"] if item["id"] == str(free))
    # Nulls, not an invented name (§15): nobody looks after this one.
    assert row["assigned_user_id"] is None
    assert row["assignee_name"] is None


async def test_unassigned_detail_is_null_on_both_fields(agency):
    tid, caller, _holder, _held, free = agency
    async with await _http(tid, caller) as c:
        body = (await c.get(f"/api/clients/{free}")).json()

    assert body["assigned_user_id"] is None
    assert body["assignee_name"] is None


async def test_whitespace_preferred_name_falls_through_to_display_name(agency):
    """A `preferred_name` of only whitespace must not win over `display_name`.

    `_assignee_name_expr` wraps each candidate in `NULLIF(btrim(...), '')` so
    a blank-but-not-null `preferred_name` is treated as absent, the same way
    `GET /api/members` treats it in Python. A bare `COALESCE` of the raw
    columns would let the whitespace win instead and render an empty name.
    """
    tid, caller, _holder, _held, _free = agency
    blank = await _user(
        tid,
        f"blank{tid.hex[:6]}@agency.sg",
        preferred_name="   ",
        display_name="Wren Ong",
    )
    client_id = await _client(tid, "Crest", blank)
    async with await _http(tid, caller) as c:
        body = (await c.get(f"/api/clients/{client_id}")).json()

    assert body["assigned_user_id"] == str(blank)
    assert body["assignee_name"] == "Wren Ong"


async def test_display_name_then_the_email_local_part(agency):
    """The two fallbacks, in the order `GET /api/members` uses."""
    tid, caller, _holder, _held, _free = agency
    by_display = await _user(
        tid, f"d{tid.hex[:8]}@agency.sg", display_name="Mei Wong"
    )
    nameless = await _user(tid, f"jo.tan{tid.hex[:6]}@agency.sg")
    one = await _client(tid, "Cedar", by_display)
    two = await _client(tid, "Delta", nameless)

    async with await _http(tid, caller) as c:
        items = {i["id"]: i for i in (await c.get("/api/clients")).json()["items"]}

    assert items[str(one)]["assignee_name"] == "Mei Wong"
    assert items[str(two)]["assignee_name"] == f"jo.tan{tid.hex[:6]}"


async def test_detail_carries_the_collaborators(agency):
    """Cover is on the detail payload, never on the list.

    A page of clients must not carry a collaborator list per row; this panel is
    only ever open for one client at a time.
    """
    tid, caller, holder, held, _free = agency
    async with await _http(tid, holder) as c:
        assert (
            await c.post(
                f"/api/clients/{held}/collaborators", json={"user_id": str(caller)}
            )
        ).status_code == 201

    async with await _http(tid, caller) as c:
        detail = (await c.get(f"/api/clients/{held}")).json()
        listed = (await c.get("/api/clients")).json()["items"]

    assert detail["collaborators"] == [
        {"user_id": str(caller), "name": f"c{tid.hex[:8]}"}
    ]
    assert all("collaborators" not in item for item in listed)
