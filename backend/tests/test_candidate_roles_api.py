# allow-hardcode: "Parkway Shenton" / "Staff Nurse" / "Jane Tan" below are
# test fixture content specified verbatim by the task brief, not a
# matching/scoring oracle.
"""Roles a candidate held. Typed by a person; nothing here is AI-derived yet."""

import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy import select, text

from app.db.rls import tenant_session
from app.main import app
from app.models.candidate import CandidateRole
from tests.conftest import AdminSessionLocal
from tests.test_opportunities_api import sign_in


async def _a_candidate_row(tenant_id, user_id):
    candidate_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email, "
                "pipeline_stage, record_status) "
                "VALUES (:i, :t, 'Jane Tan', :e, 'new', 'active')"
            ),
            {"i": candidate_id, "t": tenant_id, "e": f"jane{candidate_id.hex[:6]}@acme.sg"},
        )
        await s.commit()
    return candidate_id


@pytest.fixture
async def agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    yield tid, uid
    async with AdminSessionLocal() as s:
        for table in (
            "candidate_roles",
            "candidate_field_overrides",
            "candidate_skills",
            "candidates",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.fixture
async def other_agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    yield tid, uid
    async with AdminSessionLocal() as s:
        for table in (
            "candidate_roles",
            "candidate_field_overrides",
            "candidate_skills",
            "candidates",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.mark.asyncio
async def test_a_role_belongs_to_one_tenant_only(agency, other_agency):
    """Agency B cannot see Agency A's role even knowing its id.

    `agency` and `other_agency` each yield `(tenant_id, user_id)` and are
    defined in this module — see Task 3 Step 1 for the body. `tenant_session`
    is the real scoped session from `app.db.rls`, the same one the API uses.
    """
    a_tenant, a_user = agency
    b_tenant, _b_user = other_agency
    a_candidate = await _a_candidate_row(a_tenant, a_user)

    async with tenant_session(a_tenant) as session:
        session.add(
            CandidateRole(
                tenant_id=a_tenant,
                candidate_id=a_candidate,
                employer="Parkway Shenton",
                employer_normalized="parkway shenton",
                title="Staff Nurse",
                title_normalized="staff nurse",
                started_on=None,
                started_precision="month",
                source=CandidateRole.HUMAN,
                status=CandidateRole.CONFIRMED,
            )
        )
        await session.commit()

    async with tenant_session(b_tenant) as session:
        rows = (await session.execute(select(CandidateRole))).scalars().all()
        assert rows == []


async def _client_for(tid, uid) -> httpx.AsyncClient:
    """A signed-in client for one agency.

    The cookie helper is imported rather than copied: a second implementation
    of the session format would keep passing after the real one changed.
    """
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    sign_in(c, uid, tid)
    return c


async def _a_candidate(client, **fields) -> dict:
    res = await client.post("/api/candidates", json=fields)
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_adding_a_role_updates_the_candidate_row(agency):
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")

        res = await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={
                "employer": "Raffles Medical",
                "title": "Enrolled Nurse",
                "started_on": "2019-03-01",
                "started_precision": "month",
            },
        )
        assert res.status_code == 201, res.text

        again = await client.get(f"/api/candidates/{candidate['id']}")
        body = again.json()
    assert body["current_employer"] == "Raffles Medical"
    assert body["current_title"] == "Enrolled Nurse"
    assert len(body["roles"]) == 1
    assert body["is_current"] is True


@pytest.mark.asyncio
async def test_a_role_that_ends_before_it_starts_is_a_422(agency):
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        res = await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={
                "employer": "Raffles Medical",
                "title": "Enrolled Nurse",
                "started_on": "2020-01-01",
                "ended_on": "2019-01-01",
            },
        )
    assert res.status_code == 422
    assert "end" in res.json()["detail"][0]["msg"].lower()


@pytest.mark.asyncio
async def test_another_agencys_candidate_is_a_404_not_a_403(agency, other_agency):
    """Existence is not disclosed. A 403 would confirm the id is real."""
    async with await _client_for(*agency) as client:
        other_tenant_candidate_id = await _a_candidate_row(*other_agency)
        payload = {"employer": "Coda", "title": "Engineer"}
        res = await client.post(
            f"/api/candidates/{other_tenant_candidate_id}/roles", json=payload
        )
        # The identical body against the caller's own candidate, so the 404
        # above is the tenant boundary and not a route that does not exist.
        mine = await _a_candidate(client, full_name="Tan Hui Ling")
        ours = await client.post(f"/api/candidates/{mine['id']}/roles", json=payload)
    assert res.status_code == 404
    assert ours.status_code == 201


@pytest.mark.asyncio
async def test_a_years_experience_override_survives_derivation(agency):
    """A person asserted this. Adding a role must not quietly overwrite it."""
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        await client.patch(f"/api/candidates/{candidate['id']}", json={"years_experience": 20})
        await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={
                "employer": "Raffles Medical",
                "title": "Enrolled Nurse",
                "started_on": "2024-01-01",
                "started_precision": "month",
            },
        )
        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["years_experience"] == 20


@pytest.mark.asyncio
async def test_deleting_the_last_role_leaves_the_cached_columns_alone(agency):
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        created = (
            await client.post(
                f"/api/candidates/{candidate['id']}/roles",
                json={
                    "employer": "Raffles Medical",
                    "title": "Enrolled Nurse",
                    "started_on": "2019-03-01",
                    "started_precision": "month",
                },
            )
        ).json()

        gone = await client.delete(f"/api/candidates/{candidate['id']}/roles/{created['id']}")
        assert gone.status_code == 204

        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["roles"] == []
    assert body["current_employer"] == "Raffles Medical"


@pytest.mark.asyncio
async def test_roles_come_back_current_first_then_newest(agency):
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        for employer, started, ended in [
            ("Oldest", "2010-01-01", "2013-01-01"),
            ("Middle", "2015-01-01", "2018-01-01"),
            ("Current", "2019-01-01", None),
        ]:
            payload = {
                "employer": employer,
                "title": "Nurse",
                "started_on": started,
                "started_precision": "month",
            }
            if ended:
                payload["ended_on"] = ended
                payload["ended_precision"] = "month"
            res = await client.post(f"/api/candidates/{candidate['id']}/roles", json=payload)
            assert res.status_code == 201, res.text

        roles = (await client.get(f"/api/candidates/{candidate['id']}")).json()["roles"]
    assert [r["employer"] for r in roles] == ["Current", "Middle", "Oldest"]


@pytest.mark.asyncio
async def test_an_open_ended_role_keeps_accruing_between_reads(agency):
    """The GET derives rather than trusting the column.

    A role with no end date gains a month every month, so a value written once
    at role-creation is wrong by the following month. Nothing touches the role
    between the POST and the GET here — the two-year answer has to come from
    the recompute.
    """
    two_years_ago = date(date.today().year - 2, date.today().month, 1)
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={
                "employer": "Raffles Medical",
                "title": "Enrolled Nurse",
                "started_on": two_years_ago.isoformat(),
                "started_precision": "month",
            },
        )
        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["years_experience"] == 2


@pytest.mark.asyncio
async def test_editing_a_role_moves_the_candidates_current_employer(agency):
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        created = (
            await client.post(
                f"/api/candidates/{candidate['id']}/roles",
                json={
                    "employer": "Raffles Medical",
                    "title": "Enrolled Nurse",
                    "started_on": "2019-03-01",
                    "started_precision": "month",
                },
            )
        ).json()
        patched = await client.patch(
            f"/api/candidates/{candidate['id']}/roles/{created['id']}",
            json={"employer": "Parkway Shenton", "title": "Staff Nurse"},
        )
        assert patched.status_code == 200, patched.text
        missing = await client.patch(
            f"/api/candidates/{candidate['id']}/roles/{uuid.uuid4()}",
            json={"employer": "Nowhere", "title": "Nobody"},
        )
        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert missing.status_code == 404
    assert body["current_employer"] == "Parkway Shenton"
    assert body["roles"][0]["employer_normalized"] == "parkway shenton"


# allow-hardcode: "Raffles Medical" / "Parkway Shenton" / "Enrolled Nurse" /
# "Ward nursing" below are test fixture content, same as the file header note,
# not a matching/scoring oracle.
@pytest.mark.asyncio
async def test_patching_only_the_employer_leaves_the_rest_of_the_role_alone(agency):
    """PATCH means "change what I sent", not "replace with what I sent".

    Regression test for a bug where the route accepted a full role body and
    `setattr` every field, so an absent key became `None` instead of "leave
    unchanged" — a patch of just `{employer}` used to silently wipe the
    dates. This is the test that would have caught it.
    """
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        created = (
            await client.post(
                f"/api/candidates/{candidate['id']}/roles",
                json={
                    "employer": "Raffles Medical",
                    "title": "Enrolled Nurse",
                    "started_on": "2019-03-01",
                    "started_precision": "month",
                    "ended_on": "2022-06-01",
                    "ended_precision": "month",
                    "location": "Singapore",
                    "description": "Ward nursing",
                },
            )
        ).json()

        patched = await client.patch(
            f"/api/candidates/{candidate['id']}/roles/{created['id']}",
            json={"employer": "Parkway Shenton"},
        )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["employer"] == "Parkway Shenton"
    assert body["title"] == "Enrolled Nurse"
    assert body["started_on"] == "2019-03-01"
    assert body["started_precision"] == "month"
    assert body["ended_on"] == "2022-06-01"
    assert body["location"] == "Singapore"
    assert body["description"] == "Ward nursing"


@pytest.mark.asyncio
async def test_a_patch_that_ends_before_the_stored_start_is_a_422(agency):
    """The cross-field check must run against the merged row, not the body.

    A patch of only `{ended_on}` never mentions `started_on` at all, so the
    comparison has to pull the stored start date off the row after merging —
    validating the incoming body alone would miss this entirely.
    """
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        created = (
            await client.post(
                f"/api/candidates/{candidate['id']}/roles",
                json={
                    "employer": "Raffles Medical",
                    "title": "Enrolled Nurse",
                    "started_on": "2020-01-01",
                },
            )
        ).json()

        res = await client.patch(
            f"/api/candidates/{candidate['id']}/roles/{created['id']}",
            json={"ended_on": "2019-01-01"},
        )
    assert res.status_code == 422
    assert "end" in res.json()["detail"][0]["msg"].lower()


@pytest.mark.asyncio
async def test_a_candidate_with_only_rejected_roles_has_no_derived_profile(agency):
    """`derive` drops rejected roles; the "has roles" guard must agree.

    Inserting the role directly with `status=REJECTED` bypasses the create
    route (which always stamps `CONFIRMED`), then a PATCH on that same role
    exercises `apply_derived` and proves it takes the "no roles" branch
    rather than deriving from an empty, all-rejected set.
    """
    tenant_id, _user_id = agency
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")

        async with tenant_session(tenant_id) as session:
            role = CandidateRole(
                tenant_id=tenant_id,
                candidate_id=uuid.UUID(candidate["id"]),
                employer="Raffles Medical",
                employer_normalized="raffles medical",
                title="Enrolled Nurse",
                title_normalized="enrolled nurse",
                started_on=date(2019, 3, 1),
                started_precision="month",
                source=CandidateRole.HUMAN,
                status=CandidateRole.REJECTED,
            )
            session.add(role)
            await session.commit()
            role_id = role.id

        patched = await client.patch(
            f"/api/candidates/{candidate['id']}/roles/{role_id}",
            json={"employer": "Parkway Shenton"},
        )
        assert patched.status_code == 200, patched.text
        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["current_employer"] is None
    assert body["current_title"] is None
