"""The candidate list, as a recruiter reads it.

Counts are computed over the whole tenant rather than the page: a chip that
shrank as you paged would answer a different question than it appears to.
Merged rows are hidden by default because a merged row is not a person any
more, but stay reachable by id so an unmerge is still possible.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in  # the real session cookie, not a copy


@pytest.fixture
async def agency_with_candidates():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {"active": uuid.uuid4(), "placed": uuid.uuid4(), "merged": uuid.uuid4()}
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        rows = [
            (ids["active"], "Jane Tan", "jane@acme.sg", "new", "active", None),
            (ids["placed"], "John Lim", "john@acme.sg", "placed", "active", None),
            (ids["merged"], "Jane T", "jane.t@acme.sg", "new", "merged", ids["active"]),
        ]
        for cid, name, email, stage, status, target in rows:
            await s.execute(
                text(
                    "INSERT INTO candidates (id, tenant_id, full_name, email, "
                    "pipeline_stage, record_status, merged_into_candidate_id) "
                    "VALUES (:i, :t, :n, :e, :st, :rs, :mt)"
                ),
                {"i": cid, "t": tid, "n": name, "e": email, "st": stage,
                 "rs": status, "mt": target},
            )
        await s.commit()
    yield tid, uid, ids
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE candidates SET record_status = 'active', "
                "merged_into_candidate_id = NULL WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
        for table in ("candidate_field_overrides", "candidate_skills", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_list_hides_merged_rows(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates")).json()
    assert {row["id"] for row in body["items"]} == {str(ids["active"]), str(ids["placed"])}


async def test_the_stage_filter_narrows_the_list(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?pipeline_stage=placed")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["placed"])]


async def test_counts_are_tenant_wide_not_page_wide(agency_with_candidates) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        first = (await http.get("/api/candidates?limit=1&offset=0")).json()
        second = (await http.get("/api/candidates?limit=1&offset=1")).json()
    assert first["counts"] == second["counts"]
    assert len(first["items"]) == 1


async def test_search_finds_a_candidate_by_email(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?q=john@acme.sg")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["placed"])]


async def test_merged_rows_are_reachable_by_explicit_filter(agency_with_candidates) -> None:
    """Otherwise a wrongly merged person cannot be found to unmerge them."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?record_status=merged")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["merged"])]


async def test_a_merged_candidate_is_still_reachable_by_id(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.get(f"/api/candidates/{ids['merged']}")
    assert r.status_code == 200
    assert r.json()["record_status"] == "merged"


async def test_another_agencys_candidate_is_a_404_not_a_403(agency_with_candidates) -> None:
    """403 would confirm the id exists, which is itself a disclosure."""
    _tid, _uid, ids = agency_with_candidates
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
            r = await http.get(f"/api/candidates/{ids['active']}")
            listing = (await http.get("/api/candidates")).json()
        assert r.status_code == 404
        assert listing["items"] == []
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": other_tid})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tid})
            await s.commit()


async def test_search_with_percent_wildcard_returns_no_results(agency_with_candidates) -> None:
    """A literal % in search should not be treated as a wildcard."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?q=%")).json()
    # Should return no candidates, not all of them
    assert body["items"] == []
    assert body["total"] == 0


async def test_search_finds_candidate_with_literal_percent(agency_with_candidates) -> None:
    """A candidate whose name contains % should be findable by literal % search."""
    tid, uid, ids = agency_with_candidates
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email, "
                "pipeline_stage, record_status, merged_into_candidate_id) "
                "VALUES (:i, :t, :n, :e, :st, :rs, :mt)"
            ),
            {"i": cid, "t": tid, "n": "Alex 100% Ready", "e": "alex@test.sg",
             "st": "new", "rs": "active", "mt": None},
        )
        await s.commit()
    try:
        async with await _client_for(tid, uid) as http:
            body = (await http.get("/api/candidates?q=100%")).json()
        # Should find the candidate with 100% in their name
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] == str(cid)
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM candidates WHERE id = :i"), {"i": cid})
            await s.commit()


async def test_search_with_underscore_does_not_match_any_character(agency_with_candidates) -> None:
    """A literal _ in search should not be treated as a single-char wildcard."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?q=_")).json()
    # Should return no candidates (none have _ in name/email/phone)
    assert body["items"] == []
    assert body["total"] == 0
