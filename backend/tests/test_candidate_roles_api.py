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
from app.services.cv.persist import persist_cv
from app.services.cv.schema import CVResponse
from app.services.llm.client import LLMResult
from tests.conftest import AdminSessionLocal
from tests.test_opportunities_api import sign_in

# A minimal CV, and the pieces `persist_cv` needs to turn one role of it into
# a `candidate_roles` row plus its `extraction_evidence`. Kept local rather
# than imported from `test_cv_persist` — that module imports `agency` and
# `_a_candidate_row` from this one, so the reverse import would be a cycle.
_CV_TEXT = "Jane Tan\nStaff Nurse, Parkway Shenton, Mar 2019 to Mar 2020\n"


def _cv_field(value: str, evidence: str | None = None) -> dict:
    """One extracted field, with the span `verify` will check against `_CV_TEXT`.

    `evidence` defaults to `value` (found verbatim in `_CV_TEXT`); passing a
    string that is not on the page is how a test manufactures the invalid
    case without touching `verify` itself.
    """
    quote = evidence if evidence is not None else value
    at = _CV_TEXT.find(quote)
    return {
        "value": value,
        "evidence": quote,
        "start_char": max(at, 0),
        "end_char": max(at, 0) + len(quote),
        "confidence": 0.9,
    }


def _cv_role(
    title_evidence: str | None = None, company_evidence: str | None = None
) -> tuple[CVResponse, LLMResult]:
    payload = {
        "roles": [{"title": _cv_field("Staff Nurse", title_evidence),
                    "company": _cv_field("Parkway Shenton", company_evidence)}],
        "skills": [],
    }
    return CVResponse.model_validate(payload), LLMResult(
        data=payload, model="test/fast", latency_ms=12, raw={"choices": []}
    )


async def _cv_document(tenant_id, candidate_id) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_documents (id, tenant_id, candidate_id, filename,"
                " content_type, byte_size, object_key, parse_state)"
                " VALUES (:i, :t, :c, 'cv.pdf', 'application/pdf', 10, :k, 'parsing')"
            ),
            {"i": document_id, "t": tenant_id, "c": candidate_id, "k": f"{tenant_id}/cv.pdf"},
        )
        await s.commit()
    return document_id


async def _persist_cv_role(
    tenant_id, candidate_id, title_evidence=None, company_evidence=None
) -> uuid.UUID:
    """Runs the real CV pipeline so the resulting evidence row is the same
    shape production writes, then hands back the new role's id."""
    from app.models.candidate import CandidateDocument

    document_id = await _cv_document(tenant_id, candidate_id)
    response, result = _cv_role(title_evidence, company_evidence)
    async with tenant_session(tenant_id) as session:
        document = await session.get(CandidateDocument, document_id)
        await persist_cv(
            session,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            document=document,
            response=response,
            result=result,
            text=_CV_TEXT,
        )
        # Queried inside the same block, not after: `tenant_session` sets the
        # RLS tenant with `SET LOCAL`, which a commit discards, and the
        # context manager's own commit only fires once this block exits.
        role_id = (
            await session.execute(
                select(CandidateRole.id).where(CandidateRole.candidate_id == candidate_id)
            )
        ).scalar_one()
    return role_id


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
            "extraction_evidence",
            "extractions",
            "candidate_documents",
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
            "extraction_evidence",
            "extractions",
            "candidate_documents",
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
async def test_deleting_the_last_role_clears_the_cached_columns(agency):
    """The stale value came from the very role the recruiter just removed.

    If it was deleted because it was wrong, keeping the derived value would
    preserve exactly the wrong data, and with no override recorded it would be
    indistinguishable from truth. Better to show nothing than a fact nobody
    can vouch for.
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
                },
            )
        ).json()

        gone = await client.delete(f"/api/candidates/{candidate['id']}/roles/{created['id']}")
        assert gone.status_code == 204

        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["roles"] == []
    assert body["current_employer"] is None
    assert body["current_title"] is None
    assert body["years_experience"] is None


@pytest.mark.asyncio
async def test_deleting_the_last_role_preserves_an_override(agency):
    """An overridden field is a person's own assertion, not derivation's.

    Clearing the derived columns on last-role deletion must still leave an
    overridden `years_experience` untouched — the override is deliberately
    outside derivation's authority in both directions.
    """
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        await client.patch(f"/api/candidates/{candidate['id']}", json={"years_experience": 20})
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
    assert body["current_employer"] is None
    assert body["years_experience"] == 20


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
async def test_an_undated_remaining_role_clears_years_experience(agency):
    """`derive` returns `years_experience=None` even with a live role, when
    that role has no dates to span. The cached number must not survive that —
    it was derived from a role that no longer supports it.
    """
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        dated = (
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
        await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={"employer": "Parkway Shenton", "title": "Staff Nurse"},
        )

        gone = await client.delete(f"/api/candidates/{candidate['id']}/roles/{dated['id']}")
        assert gone.status_code == 204

        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["years_experience"] is None


@pytest.mark.asyncio
async def test_an_overridden_years_experience_survives_an_undated_remainder(agency):
    """Same setup as above, but the human asserted a number — it must stay."""
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        await client.patch(f"/api/candidates/{candidate['id']}", json={"years_experience": 20})
        dated = (
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
        await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={"employer": "Parkway Shenton", "title": "Staff Nurse"},
        )

        gone = await client.delete(f"/api/candidates/{candidate['id']}/roles/{dated['id']}")
        assert gone.status_code == 204

        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["years_experience"] == 20


@pytest.mark.asyncio
async def test_deleting_the_current_role_falls_back_to_the_next_most_recent(agency):
    """Deleting only the *current* role, with an older one still on file.

    `current_employer`/`current_title` must move to the surviving role rather
    than being cleared (there is still a source for them) or left stale
    (pointing at the role that was just deleted).
    """
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        older = (
            await client.post(
                f"/api/candidates/{candidate['id']}/roles",
                json={
                    "employer": "Raffles Medical",
                    "title": "Enrolled Nurse",
                    "started_on": "2015-01-01",
                    "started_precision": "month",
                    "ended_on": "2019-01-01",
                    "ended_precision": "month",
                },
            )
        ).json()
        current = (
            await client.post(
                f"/api/candidates/{candidate['id']}/roles",
                json={
                    "employer": "Parkway Shenton",
                    "title": "Staff Nurse",
                    "started_on": "2019-06-01",
                    "started_precision": "month",
                },
            )
        ).json()

        gone = await client.delete(f"/api/candidates/{candidate['id']}/roles/{current['id']}")
        assert gone.status_code == 204

        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["current_employer"] == "Raffles Medical"
    assert body["current_title"] == "Enrolled Nurse"
    assert body["is_current"] is False
    assert older["employer"] == "Raffles Medical"  # sanity: the surviving role


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


@pytest.mark.asyncio
async def test_a_cv_role_serializes_with_its_quoted_evidence(agency):
    """The disclosure this feature exists for: a role the CV parser proposed
    carries the line of the CV that produced it, once `evidence_valid` says
    that line is really on the page."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    role_id = await _persist_cv_role(tenant_id, candidate_id)

    async with await _client_for(tenant_id, user_id) as client:
        body = (await client.get(f"/api/candidates/{candidate_id}")).json()

    role = next(r for r in body["roles"] if r["id"] == str(role_id))
    assert role["evidence"] == "Staff Nurse"


@pytest.mark.asyncio
async def test_a_human_typed_role_serializes_with_no_evidence(agency):
    """A recruiter's own entry has no `extraction_evidence` row at all — the
    key must be absent, not an empty string the frontend would render as a
    blank disclosure."""
    async with await _client_for(*agency) as client:
        candidate = await _a_candidate(client, full_name="Tan Hui Ling")
        res = await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={"employer": "Parkway Shenton", "title": "Staff Nurse"},
        )
        assert res.status_code == 201, res.text
        assert "evidence" not in res.json()

        body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert "evidence" not in body["roles"][0]


@pytest.mark.asyncio
async def test_unverified_evidence_is_not_exposed(agency):
    """A quote that failed verification is precisely the thing §15 forbids
    asserting — it must not reach the client even though the row exists."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    # Neither field's "evidence" is a string actually printed in `_CV_TEXT`,
    # so `verify` marks both invalid — the role exists, but nothing about it
    # was ever confirmed on the page.
    role_id = await _persist_cv_role(
        tenant_id,
        candidate_id,
        title_evidence="Ward Manager",
        company_evidence="Some Other Clinic",
    )

    async with await _client_for(tenant_id, user_id) as client:
        body = (await client.get(f"/api/candidates/{candidate_id}")).json()

    role = next(r for r in body["roles"] if r["id"] == str(role_id))
    assert "evidence" not in role


@pytest.mark.asyncio
async def test_a_role_evidence_belongs_to_one_tenant_only(agency, other_agency):
    """Agency B's session cannot pull Agency A's evidence, even knowing the
    role id — the same RLS boundary the roles themselves are scoped by."""
    from app.api.candidate_roles import evidence_for

    a_tenant, a_user = agency
    b_tenant, _b_user = other_agency
    a_candidate = await _a_candidate_row(a_tenant, a_user)
    role_id = await _persist_cv_role(a_tenant, a_candidate)

    async with tenant_session(a_tenant) as session:
        assert (await evidence_for(session, [role_id])).get(role_id) == "Staff Nurse"

    async with tenant_session(b_tenant) as session:
        assert (await evidence_for(session, [role_id])) == {}
