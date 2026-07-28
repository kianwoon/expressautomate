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
        # No status/pointer reset before the delete: a test may leave two rows
        # sharing an email where one is merged, and resetting both to 'active'
        # first would trip `uq_candidates_tenant_email` before the delete ever
        # runs. Deleting the whole tenant's rows in one statement is safe
        # regardless of merge pointers — `fk_candidates_merged_into_same_tenant`
        # is ON DELETE CASCADE, and a single DELETE evaluates its row set
        # before enforcing the FK, so self-references within the same
        # tenant never block it.
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


async def test_creating_a_candidate_records_who_did_it(agency_with_candidates) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            "/api/candidates",
            json={"full_name": "New Person", "email": "new@acme.sg", "skills": ["Python"]},
        )
    assert r.status_code == 201
    async with AdminSessionLocal() as s:
        created_by = (
            await s.execute(
                text("SELECT created_by FROM candidates WHERE id = :i"),
                {"i": uuid.UUID(r.json()["id"])},
            )
        ).scalar_one()
    assert created_by == uid


async def test_creating_a_duplicate_email_is_a_conflict_not_a_500(agency_with_candidates) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post("/api/candidates", json={"full_name": "X", "email": "jane@acme.sg"})
    assert r.status_code == 409


async def test_a_split_identity_is_refused_with_both_names(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        await http.patch(
            f"/api/candidates/{ids['placed']}", json={"phone_raw": "+65 9123 4567"}
        )
        r = await http.post(
            "/api/candidates",
            json={"full_name": "Z", "email": "jane@acme.sg", "phone_raw": "+65 9123 4567"},
        )
    assert r.status_code == 409
    assert "jane@acme.sg" in r.text or str(ids["active"]) in r.text


async def test_editing_a_field_records_an_override(agency_with_candidates) -> None:
    """This is what stops a later import undoing a recruiter's correction."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        await http.patch(
            f"/api/candidates/{ids['active']}", json={"current_title": "Senior Engineer"}
        )
        body = (await http.get(f"/api/candidates/{ids['active']}")).json()
    assert body["current_title"] == "Senior Engineer"
    assert "current_title" in body["overridden_fields"]


async def test_a_recruiter_may_archive_but_not_delete(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE users SET role = 'recruiter' WHERE id = :i"), {"i": uid}
        )
        await s.commit()
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/candidates/{ids['active']}/archive")).status_code == 200
        assert (await http.delete(f"/api/candidates/{ids['active']}")).status_code == 403


async def test_an_owner_may_delete(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates  # fixture creates this user as owner
    async with await _client_for(tid, uid) as http:
        assert (await http.delete(f"/api/candidates/{ids['placed']}")).status_code == 204
        assert (await http.get(f"/api/candidates/{ids['placed']}")).status_code == 404


async def test_export_returns_every_stored_field(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get(f"/api/candidates/{ids['active']}/export")).json()
    assert body["email"] == "jane@acme.sg"
    assert "skills" in body


async def test_merge_moves_skills_and_frees_both_keys(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/candidates/{ids['placed']}/merge",
            json={"target_id": str(ids["active"])},
        )
        assert r.status_code == 200
        # The loser's email is free again, so a new person may take it.
        created = await http.post(
            "/api/candidates", json={"full_name": "Someone New", "email": "john@acme.sg"}
        )
    assert created.status_code == 201


async def test_a_candidate_cannot_be_merged_into_itself(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/candidates/{ids['active']}/merge",
            json={"target_id": str(ids["active"])},
        )
    assert r.status_code == 400
