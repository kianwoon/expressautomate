"""The candidate list, as a recruiter reads it.

Counts are computed over the whole tenant rather than the page: a chip that
shrank as you paged would answer a different question than it appears to.
Merged rows are hidden by default because a merged row is not a person any
more, but stay reachable by id so an unmerge is still possible.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import candidates as candidates_api
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


async def test_editing_email_to_one_another_candidate_holds_is_a_conflict(
    agency_with_candidates,
) -> None:
    """Covers the PATCH `IntegrityError` backstop at `app/api/candidates.py`.

    The POST path already has a duplicate-email test above; this exercises
    the same unique index from the update path, where the collision is
    caught in `update_candidate`'s own `except IntegrityError` block rather
    than `create_candidate`'s.
    """
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.patch(f"/api/candidates/{ids['placed']}", json={"email": "jane@acme.sg"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Another candidate already has that email or phone"


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


async def test_a_multi_field_patch_only_overrides_what_changed(agency_with_candidates) -> None:
    """The real form sends every field on every edit, not just the one that

    changed. `exclude_unset` cannot help there — the client sets them all —
    so the backend must be the one comparing the incoming value against what
    the row already held. A recruiter fixing one typo must not freeze the
    other thirteen fields from a later import.
    """
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        before = (await http.get(f"/api/candidates/{ids['active']}")).json()
        body = {
            "full_name": before["full_name"],
            "email": before["email"],
            "phone_raw": before["phone_raw"],
            "current_title": "Senior Engineer",  # the only real change
            "current_employer": before["current_employer"],
            "location": before["location"],
            "years_experience": before["years_experience"],
            "expected_salary": before["expected_salary"],
            "salary_currency": before["salary_currency"],
            "salary_period": before["salary_period"],
            "available_from": before["available_from"],
            "notice_period_raw": before["notice_period_raw"],
            "employment_type": before["employment_type"],
            "notes": before["notes"],
            "pipeline_stage": before["pipeline_stage"],
        }
        await http.patch(f"/api/candidates/{ids['active']}", json=body)
        after = (await http.get(f"/api/candidates/{ids['active']}")).json()
    assert after["overridden_fields"] == ["current_title"]


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


async def test_archive_then_restore_returns_to_the_default_list(agency_with_candidates) -> None:
    """Archiving is what recruiters do daily, and the spec's whole rationale

    for making delete owner-only is that archiving is reversible. Restore is
    the other half of that promise.
    """
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(f"/api/candidates/{ids['active']}/archive")
        assert r.status_code == 200
        body = (await http.get(f"/api/candidates/{ids['active']}")).json()
        assert body["record_status"] == "archived"

        r = await http.post(f"/api/candidates/{ids['active']}/restore")
        assert r.status_code == 200
        assert r.json()["record_status"] == "active"
        body = (await http.get(f"/api/candidates/{ids['active']}")).json()
        assert body["record_status"] == "active"


async def test_restoring_a_merged_candidate_is_refused(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(f"/api/candidates/{ids['merged']}/restore")
    assert r.status_code == 400


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


async def test_a_clean_unmerge_restores_the_candidate(agency_with_candidates) -> None:
    """No one else has taken the email or phone, so the unmerge just succeeds."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        merge = await http.post(
            f"/api/candidates/{ids['placed']}/merge",
            json={"target_id": str(ids["active"])},
        )
        assert merge.status_code == 200

        r = await http.post(f"/api/candidates/{ids['placed']}/unmerge")
    assert r.status_code == 200
    assert r.json()["record_status"] == "active"

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT record_status, merged_into_candidate_id FROM candidates "
                    "WHERE id = :i"
                ),
                {"i": ids["placed"]},
            )
        ).first()
    assert row.record_status == "active"
    assert row.merged_into_candidate_id is None


async def test_unmerge_is_blocked_when_the_key_was_taken(agency_with_candidates) -> None:
    """Someone else now holds the loser's email — a 409 naming them, not a 500."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        merge = await http.post(
            f"/api/candidates/{ids['placed']}/merge",
            json={"target_id": str(ids["active"])},
        )
        assert merge.status_code == 200

        # Merging freed john@acme.sg for reuse; a new candidate now takes it.
        created = await http.post(
            "/api/candidates", json={"full_name": "New Holder", "email": "john@acme.sg"}
        )
        assert created.status_code == 201
        new_id = created.json()["id"]

        r = await http.post(f"/api/candidates/{ids['placed']}/unmerge")
    assert r.status_code == 409
    assert new_id in r.text

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT record_status FROM candidates WHERE id = :i"),
                {"i": ids["placed"]},
            )
        ).first()
    assert row.record_status == "merged"


async def test_merging_a_merge_target_repoints_the_chain(agency_with_candidates) -> None:
    """A -> B -> C must collapse to A -> C and B -> C, never a two-hop chain."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        # ids['merged'] already points at ids['active']. Now merge ids['active']
        # into ids['placed']: the row already pointing at 'active' must be
        # re-pointed at 'placed' instead of being left in a chain.
        r = await http.post(
            f"/api/candidates/{ids['active']}/merge",
            json={"target_id": str(ids["placed"])},
        )
    assert r.status_code == 200

    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, record_status, merged_into_candidate_id FROM candidates "
                    "WHERE tenant_id = :t"
                ),
                {"t": tid},
            )
        ).all()
    by_id = {row.id: row for row in rows}
    assert by_id[ids["active"]].record_status == "merged"
    assert by_id[ids["active"]].merged_into_candidate_id == ids["placed"]
    assert by_id[ids["merged"]].record_status == "merged"
    assert by_id[ids["merged"]].merged_into_candidate_id == ids["placed"]
    # No row points at a merged row.
    merged_ids = {row.id for row in rows if row.record_status == "merged"}
    for row in rows:
        if row.merged_into_candidate_id is not None:
            assert row.merged_into_candidate_id not in merged_ids


# --- The bugs an adversarial pass through the real API turned up. ------------


# How the race is made deterministic rather than hoped for.
#
# Firing two opposing merges with `asyncio.gather` alone reproduces the cycle
# only sometimes — whether both transactions read before either writes depends
# on how the event loop happens to interleave two sets of database round
# trips, and a test that reports the bug only sometimes is a test that will be
# ignored. So `_load` is slowed by a fixed pause: every request finishes both
# of its reads before any request reaches its writes, which is precisely the
# interleaving the bug needs.
#
# The pause does not hide the fix. With the rows locked before they are read,
# the second request cannot reach `_load` at all until the first has committed
# and released them, so it re-reads the world the first request left behind
# and refuses. Without the locks, both read the same stale snapshot and both
# commit.
_RACE_PAUSE_SECONDS = 0.15


async def _new_candidate(http, **fields) -> str:
    r = await http.post("/api/candidates", json={"full_name": "Nobody", **fields})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_two_opposing_merges_at_once_cannot_make_a_cycle(
    agency_with_candidates, monkeypatch
) -> None:
    """A→B and B→A raced: both used to win, leaving two rows pointing at each

    other. Neither then appeared in any list, neither matched, and neither
    could be unmerged into a live record — the pair was simply gone.
    """
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        a = await _new_candidate(http, full_name="A", email="a@race.sg")
        b = await _new_candidate(http, full_name="B", email="b@race.sg")

    real_load = candidates_api._load

    async def paced_load(session, candidate_id):
        row = await real_load(session, candidate_id)
        await asyncio.sleep(_RACE_PAUSE_SECONDS)
        return row

    monkeypatch.setattr(candidates_api, "_load", paced_load)

    async with await _client_for(tid, uid) as one, await _client_for(tid, uid) as two:
        first, second = await asyncio.gather(
            one.post(f"/api/candidates/{a}/merge", json={"target_id": b}),
            two.post(f"/api/candidates/{b}/merge", json={"target_id": a}),
        )

    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 400], (first.text, second.text)

    async with AdminSessionLocal() as s:
        pointers = (
            await s.execute(
                text(
                    "SELECT merged_into_candidate_id FROM candidates WHERE id IN (:a, :b)"
                ),
                {"a": uuid.UUID(a), "b": uuid.UUID(b)},
            )
        ).scalars().all()
    # Exactly one pointer, so the graph is still one hop deep and the survivor
    # is a live record the loser can be unmerged back out of.
    assert len([p for p in pointers if p is not None]) == 1


async def test_a_salary_larger_than_the_column_is_a_422_not_a_500(
    agency_with_candidates,
) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        created = await http.post(
            "/api/candidates", json={"full_name": "Rich", "expected_salary": 1e10}
        )
        patched = await http.patch(
            f"/api/candidates/{ids['active']}", json={"expected_salary": 1e10}
        )
        ok = await http.patch(
            f"/api/candidates/{ids['active']}", json={"expected_salary": 9_999_999_999.99}
        )
    assert created.status_code == 422
    assert patched.status_code == 422
    assert ok.status_code == 200


async def test_an_unparseable_email_is_refused_rather_than_dropped(
    agency_with_candidates,
) -> None:
    """It used to return 201 with `email: null` — the recruiter believed they

    had stored the identity key the whole matcher depends on, and nothing had.
    """
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        created = await http.post(
            "/api/candidates", json={"full_name": "Typo", "email": "not-an-email"}
        )
        patched = await http.patch(
            f"/api/candidates/{ids['active']}", json={"email": "not-an-email"}
        )
        # An email nobody supplied is still absent, not an error.
        omitted = await http.post("/api/candidates", json={"full_name": "No Email"})
        cleared = await http.patch(f"/api/candidates/{ids['placed']}", json={"email": None})
    assert created.status_code == 422
    assert "email" in created.text
    assert patched.status_code == 422
    assert omitted.status_code == 201 and omitted.json()["email"] is None
    assert cleared.status_code == 200 and cleared.json()["email"] is None


async def test_an_unparseable_phone_is_refused_rather_than_dropped(
    agency_with_candidates,
) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        rubbish = await http.post(
            "/api/candidates", json={"full_name": "Typo", "phone_raw": "not-a-phone"}
        )
        # A fixed line still parses. It is kept and simply never identifies
        # anyone, which is the deliberate behaviour of `_identity_phone`.
        fixed_line = await http.post(
            "/api/candidates", json={"full_name": "Switchboard", "phone_raw": "+65 6123 4567"}
        )
    assert rubbish.status_code == 422
    assert fixed_line.status_code == 201
    assert fixed_line.json()["phone_e164"] is None


async def test_a_blank_name_is_a_422_naming_the_field_not_a_false_conflict(
    agency_with_candidates,
) -> None:
    """It used to surface as 409 "Already recorded" — a lie that sends someone

    hunting for a duplicate that does not exist.
    """
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        created = await http.post("/api/candidates", json={"full_name": "   "})
        patched = await http.patch(f"/api/candidates/{ids['active']}", json={"full_name": ""})
        nulled = await http.patch(
            f"/api/candidates/{ids['active']}", json={"full_name": None}
        )
    for r in (created, patched, nulled):
        assert r.status_code == 422, r.text
        assert "full_name" in r.text


async def test_a_merged_candidate_cannot_be_edited(agency_with_candidates) -> None:
    """Archive already refuses a merged row; PATCH used to edit it happily,

    including the email and phone that `unmerge` has to give back.
    """
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.patch(
            f"/api/candidates/{ids['merged']}", json={"email": "hijack@acme.sg"}
        )
        after = (await http.get(f"/api/candidates/{ids['merged']}")).json()
    assert r.status_code == 400
    assert after["email"] == "jane.t@acme.sg"


async def test_years_of_experience_must_be_a_number_somebody_could_mean(
    agency_with_candidates,
) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        negative = await http.post(
            "/api/candidates", json={"full_name": "Time Traveller", "years_experience": -5}
        )
        absurd = await http.post(
            "/api/candidates", json={"full_name": "Methuselah", "years_experience": 1995}
        )
        fine = await http.post(
            "/api/candidates", json={"full_name": "Normal", "years_experience": 12}
        )
    assert negative.status_code == 422
    assert absurd.status_code == 422
    assert fine.status_code == 201
