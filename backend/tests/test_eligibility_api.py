"""The eligibility endpoint, `placement_type` and the occupational requirement
pairing rule, at the API and the database (plan slice: job-order eligibility).

Three things this file must never get wrong:

- `placement_type` is set only by a person and never derived from a glossary
  code — `redact.py` strips `WP`/`EP`/`SP` from extracted text before a model
  reads it, so deriving a regulatory fact from that text would re-import
  exactly what redaction removes.
- A sex requirement without a reason is refused, at the API (422, before the
  database is touched) and by the CHECK constraint (asserted by name, so a
  test that only checks "it raised" cannot pass when a different constraint
  catches the write by accident).
- Agency A must never read Agency B's job order or candidate through this
  endpoint (§18).
"""

import uuid

import pytest
from asyncpg.exceptions import CheckViolationError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.main import app
from app.models import Opportunity
from tests.conftest import AdminSessionLocal
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def agency():
    tid, uid, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                " VALUES (:i, :t, :u, :m, 'user', 'inbox', 24)"
            ),
            {"i": mailbox_id, "t": tid, "u": uid, "m": f"oid-{tid.hex[:8]}"},
        )
        await s.commit()
    yield tid, uid, mailbox_id
    async with AdminSessionLocal() as s:
        for table in (
            "candidates",
            "opportunities",
            "email_messages",
            "mailboxes",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def _make_opportunity(
    tenant_id, mailbox_id, job_description: str = "", **fields
) -> uuid.UUID:
    """The ORM, not raw SQL: `review_status` and `quality_state` are NOT NULL
    with Python-side defaults, which a hand-written INSERT never fires — same
    reasoning as `make_opportunity` in `test_opportunities_api.py`."""
    email_id, opportunity_id = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, internet_message_id,"
                " received_datetime)"
                " VALUES (:i, :t, :m, :g, :n, now())"
            ),
            {
                "i": email_id,
                "t": tenant_id,
                "m": mailbox_id,
                "g": f"graph-{email_id.hex}",
                "n": f"<{email_id.hex}@example.sg>",
            },
        )
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
                email_message_id=email_id,
                job_description=job_description,
                **fields,
            )
        )
        await s.commit()
    return opportunity_id


def _constraint_of(exc: IntegrityError) -> str:
    cause = exc.orig
    while cause is not None and not isinstance(cause, CheckViolationError):
        cause = getattr(cause, "__cause__", None)
    assert isinstance(cause, CheckViolationError), f"not a CHECK violation: {exc.orig!r}"
    return cause.constraint_name


# --- placement_type is never inferred ---------------------------------------


async def test_placement_type_is_null_by_default(agency) -> None:
    tid, _uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    async with tenant_session(tid) as s:
        value = (
            await s.execute(
                text("SELECT placement_type FROM opportunities WHERE id = :i"), {"i": opp_id}
            )
        ).scalar_one()
    assert value is None


async def test_ingesting_an_email_containing_wp_leaves_placement_type_null(agency) -> None:
    """`WP` is a glossary code redacted before a model reads it (redact.py);
    nothing in the persistence path may read it back out as a placement type.
    """
    tid, _uid, mailbox_id = agency
    opp_id = await _make_opportunity(
        tid, mailbox_id, job_description="Client wants WP holder, 6 days a week."
    )
    async with tenant_session(tid) as s:
        value = (
            await s.execute(
                text("SELECT placement_type FROM opportunities WHERE id = :i"), {"i": opp_id}
            )
        ).scalar_one()
    assert value is None


async def test_set_placement_type_via_api(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            f"/api/opportunities/{opp_id}/placement-type",
            json={"placement_type": "mdw_work_permit"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["placement_type"] == "mdw_work_permit"


async def test_placement_type_rejects_unknown_value(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            f"/api/opportunities/{opp_id}/placement-type",
            json={"placement_type": "citizenship_by_decree"},
        )
    assert resp.status_code == 422


# --- occupational requirement pairing ---------------------------------------


async def test_sex_requirement_without_reason_refused_by_api(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            f"/api/opportunities/{opp_id}/occupational-requirement",
            json={"sex_requirement": "female"},
        )
    assert resp.status_code == 422


async def test_sex_requirement_with_reason_accepted(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            f"/api/opportunities/{opp_id}/occupational-requirement",
            json={
                "sex_requirement": "female",
                "sex_requirement_reason": "Intimate personal care of an elderly client.",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sex_requirement"] == "female"


async def test_sex_requirement_without_reason_refused_by_constraint(agency) -> None:
    """Bypassing the API (a raw INSERT, the same shape a script or a future
    endpoint might use) still hits the database CHECK, asserted by name."""
    tid, _uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    with pytest.raises(IntegrityError) as excinfo:
        async with tenant_session(tid) as s:
            await s.execute(
                text(
                    "UPDATE opportunities SET sex_requirement = 'female' WHERE id = :i"
                ),
                {"i": opp_id},
            )
            await s.commit()
    assert _constraint_of(excinfo.value) == "ck_opportunities_sex_requirement_has_reason"


async def test_placement_type_unknown_value_refused_by_constraint(agency) -> None:
    tid, _uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    with pytest.raises(IntegrityError) as excinfo:
        async with tenant_session(tid) as s:
            await s.execute(
                text("UPDATE opportunities SET placement_type = 'freelance' WHERE id = :i"),
                {"i": opp_id},
            )
            await s.commit()
    assert _constraint_of(excinfo.value) == "ck_opportunities_placement_type_known"


# --- the eligibility endpoint ------------------------------------------------


async def _make_candidate(http: AsyncClient, **fields) -> str:
    body = {"full_name": "Test Candidate", **fields}
    resp = await http.post("/api/candidates", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_not_assessable_when_placement_type_unset(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id)
    async with await _client_for(tid, uid) as http:
        candidate_id = await _make_candidate(http)
        resp = await http.get(f"/api/opportunities/{opp_id}/candidates/{candidate_id}/eligibility")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assessable"] is False
    assert body["findings"] == []
    assert body["placement_type"] is None


async def test_mdw_findings_cover_every_criterion(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id, placement_type="mdw_work_permit")
    async with await _client_for(tid, uid) as http:
        candidate_id = await _make_candidate(
            http,
            sex="female",
            date_of_birth="1990-01-01",
            education_years=10,
            nationality="PH",
        )
        resp = await http.get(f"/api/opportunities/{opp_id}/candidates/{candidate_id}/eligibility")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assessable"] is True
    criteria = [f["criterion"] for f in body["findings"]]
    assert criteria == ["sex", "age", "education", "nationality", "occupational_sex_requirement"]
    for f in body["findings"]:
        assert f["outcome"] in {"met", "not_met", "unknown", "not_applicable"}
        assert f["basis"] in {"regulatory", "occupational"}
    assert "evaluated_as_of" in body
    assert "eligible" not in body  # no boolean rollup


async def test_unknown_findings_for_missing_facts(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id, placement_type="mdw_work_permit")
    async with await _client_for(tid, uid) as http:
        candidate_id = await _make_candidate(http)
        resp = await http.get(f"/api/opportunities/{opp_id}/candidates/{candidate_id}/eligibility")
    body = resp.json()
    by_criterion = {f["criterion"]: f for f in body["findings"]}
    for criterion in ("sex", "age", "education", "nationality"):
        assert by_criterion[criterion]["outcome"] == "unknown", criterion


async def test_local_hire_is_not_applicable_not_unknown(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id, placement_type="local_hire")
    async with await _client_for(tid, uid) as http:
        candidate_id = await _make_candidate(http)
        resp = await http.get(f"/api/opportunities/{opp_id}/candidates/{candidate_id}/eligibility")
    body = resp.json()
    by_criterion = {f["criterion"]: f for f in body["findings"]}
    for criterion in ("sex", "age", "education", "nationality"):
        assert by_criterion[criterion]["outcome"] == "not_applicable", criterion


async def test_as_of_query_param_shifts_the_age_finding(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id, placement_type="mdw_work_permit")
    async with await _client_for(tid, uid) as http:
        candidate_id = await _make_candidate(http, sex="female", date_of_birth="1977-07-30")
        today = await http.get(
            f"/api/opportunities/{opp_id}/candidates/{candidate_id}/eligibility"
        )
        later = await http.get(
            f"/api/opportunities/{opp_id}/candidates/{candidate_id}/eligibility",
            params={"as_of": "2027-08-01"},
        )
    assert today.status_code == later.status_code == 200
    today_age = next(f for f in today.json()["findings"] if f["criterion"] == "age")
    later_age = next(f for f in later.json()["findings"] if f["criterion"] == "age")
    assert today_age["outcome"] != later_age["outcome"]


async def test_404_for_a_job_order_or_candidate_that_does_not_exist(agency) -> None:
    tid, uid, mailbox_id = agency
    opp_id = await _make_opportunity(tid, mailbox_id, placement_type="mdw_work_permit")
    async with await _client_for(tid, uid) as http:
        candidate_id = await _make_candidate(http)
        missing_candidate = await http.get(
            f"/api/opportunities/{opp_id}/candidates/{uuid.uuid4()}/eligibility"
        )
        missing_opportunity = await http.get(
            f"/api/opportunities/{uuid.uuid4()}/candidates/{candidate_id}/eligibility"
        )
    assert missing_candidate.status_code == 404
    assert missing_opportunity.status_code == 404


async def test_one_agency_cannot_read_anothers_eligibility(agency) -> None:
    """§18: neither the job order nor the candidate leaks across tenants."""
    tid_a, uid_a, mailbox_a = agency
    tid_b, uid_b, mailbox_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid_b, "n": f"agency-{tid_b.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid_b, "t": tid_b, "e": f"u{uid_b.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                " VALUES (:i, :t, :u, :m, 'user', 'inbox', 24)"
            ),
            {"i": mailbox_b, "t": tid_b, "u": uid_b, "m": f"oid-{tid_b.hex[:8]}"},
        )
        await s.commit()

    opp_b = await _make_opportunity(tid_b, mailbox_b, placement_type="mdw_work_permit")
    async with await _client_for(tid_b, uid_b) as http_b:
        candidate_b = await _make_candidate(http_b, sex="female")

    try:
        async with await _client_for(tid_a, uid_a) as http_a:
            resp = await http_a.get(
                f"/api/opportunities/{opp_b}/candidates/{candidate_b}/eligibility"
            )
        assert resp.status_code == 404
    finally:
        async with AdminSessionLocal() as s:
            for table in ("candidates", "opportunities", "email_messages", "mailboxes", "users"):
                await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid_b})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid_b})
            await s.commit()
