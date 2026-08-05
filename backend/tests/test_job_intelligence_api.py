"""The Job Intelligence API — POST to run, GET to read back.

Three things worth testing here, two of them adversarial:

- POST runs the analysis and stores it; GET reads it back and the two match.
- Another agency's job order is a 404, never a 403 — a 403 would confirm the
  row exists.
- The route is gated by `load_visible_opportunity`, so the visibility-guard AST
  test (which scans every opportunity-touching route) passes.

No test here reaches a real model: the engine's `analyze` is monkeypatched.

allow-hardcode: the SQL, model ids and responses below are test fixtures.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import job_intelligence
from app.core.config import settings
from app.main import app
from app.services.job_intelligence.engine import JobIntelligenceOutcome, JobIntelligenceStats
from app.services.job_intelligence.schema import (
    CandidatePersona,
    JDUnderstanding,
    JobIntelligenceResult,
    SearchPlan,
)
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


async def _seed_agency() -> tuple[uuid.UUID, uuid.UUID]:
    tid, uid = uuid.uuid4(), uuid.uuid4()
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
        await s.commit()
    return tid, uid


async def _drop_agency(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in ("job_intelligence", "opportunities", "email_messages", "mailboxes"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.commit()
    await cleanup_tenant(tid)


async def _opportunity(tenant_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    mailbox_id, message_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, retention_months) VALUES (:i, :t, :u, :m, 'inbox', 'user', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id, "u": user_id, "m": f"oid-{mailbox_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " internet_message_id, subject)"
                " VALUES (:i, :t, :m, :g, :g, 'Role wanted')"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id, "g": message_id.hex},
        )
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_raw, job_description, review_status, quality_state)"
                " VALUES (:i, :t, :e, 'Logistics Manager', 'Run the warehouse.',"
                " 'ready', 'likely')"
            ),
            {"i": opportunity_id, "t": tenant_id, "e": message_id},
        )
        await s.commit()
    return opportunity_id


def _outcome():
    return JobIntelligenceOutcome(
        result=JobIntelligenceResult(
            understanding=JDUnderstanding(
                role="Logistics Manager",
                business_purpose="Keep the warehouse running",
                daily_activities=[],
                work_environment="Warehouse",
                must_have_requirements=[],
                preferred_requirements=[],
                working_conditions="Onsite",
                success_characteristics=[],
                potential_challenges=[],
                confidence=0.8,
            ),
            persona=CandidatePersona(
                likely_backgrounds=["Logistics coordinator"],
                transferable_roles=[],
                transferable_industries=[],
                behaviours=[],
                communication_style="Direct",
                career_stage="Mid-level",
                motivations=[],
                salary_expectation="SGD 4,500/month",
                availability="1 month",
            ),
            search_plan=SearchPlan(
                platform="LinkedIn",
                priority=2,
                queries=['"logistics manager" AND dispatch'],
                negative_queries=["intern"],
                salary="SGD 4,500/month",
                location="Singapore",
                employment_type="Full-time",
            ),
        ),
        stats=JobIntelligenceStats(
            model="test/fast", prompt_tokens=100, completion_tokens=200, latency_ms=3000
        ),
        removed_codes=[],
    )


def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, uid, tid)
    return client


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@pytest.fixture
def stub_analyze(monkeypatch):
    """Replace the engine so no real model call is made."""
    captured: dict = {}

    async def _fake(opportunity, codes=(), *, llm=None):
        captured["opportunity"] = opportunity
        captured["codes"] = list(codes)
        return _outcome()

    monkeypatch.setattr(job_intelligence, "analyze", _fake)
    return captured


async def test_post_runs_and_get_reads_back(stub_analyze):

    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert res.status_code == 200, res.text
            payload = res.json()
            assert payload["intelligence"]["understanding"]["role"] == "Logistics Manager"
            assert payload["intelligence"]["search_plan"]["platform"] == "LinkedIn"
            # `removed_codes` and `analysed_at` sit beside `intelligence`, the
            # same shape GET returns, so a re-run and a read render identically.
            assert payload["removed_codes"] == []
            assert payload["analysed_at"] is not None

            # GET reads the stored row back, matching what POST returned.
            got = await c.get(f"/api/opportunities/{oid}/intelligence")
            assert got.status_code == 200
            got_payload = got.json()
            assert got_payload["intelligence"]["understanding"]["role"] == "Logistics Manager"
            assert got_payload["analysed_at"] is not None
    finally:
        await _drop_agency(tid)


async def test_get_returns_null_when_no_analysis(stub_analyze):

    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.get(f"/api/opportunities/{oid}/intelligence")
            assert res.status_code == 200
            assert res.json() == {"intelligence": None}
    finally:
        await _drop_agency(tid)


async def test_another_agencys_job_order_is_404(stub_analyze):

    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    # A second agency that cannot see the first's job order.
    other_tid, other_uid = await _seed_agency()
    try:
        async with _http(other_tid, other_uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert res.status_code == 404
            got = await c.get(f"/api/opportunities/{oid}/intelligence")
            assert got.status_code == 404
    finally:
        await _drop_agency(tid)
        await _drop_agency(other_tid)
