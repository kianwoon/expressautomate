"""The Job Intelligence API — POST enqueues, GET reads state.

Mirrors `test_sourcing_api.py`: `enqueue` is replaced so Redis is never
touched, and the assertions are the adversarial ones — can a caller reach
another agency's job order; does the route gate through visibility; does POST
answer 202 with a `pending` row rather than running the work inline.

No test here reaches a model or Redis.

allow-hardcode: the SQL, model ids and responses below are test fixtures.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import job_intelligence
from app.main import app
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


def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, uid, tid)
    return client


@pytest.fixture
def queued(monkeypatch):
    """Every job the route tried to enqueue. Redis is never touched."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(job_intelligence, "enqueue", _enqueue)
    return jobs


async def test_post_returns_202_and_enqueues(queued):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["state"] == "pending"
            assert body["intelligence"] is None
            assert body["failure_reason"] is None

            # The right job was enqueued with the row id and tenant.
            assert len(queued) == 1
            name, kwargs = queued[0]
            assert name == "run_job_intelligence"
            assert kwargs["opportunity_id"] == str(oid)
            assert kwargs["tenant_id"] == str(tid)
            assert "row_id" in kwargs
    finally:
        await _drop_agency(tid)


async def test_get_reads_back_the_state(queued):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            await c.post(f"/api/opportunities/{oid}/intelligence")
            got = await c.get(f"/api/opportunities/{oid}/intelligence")
            assert got.status_code == 200
            assert got.json()["state"] == "pending"
    finally:
        await _drop_agency(tid)


async def test_get_returns_null_when_no_analysis(queued):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.get(f"/api/opportunities/{oid}/intelligence")
            assert res.status_code == 200
            assert res.json() == {"intelligence": None}
    finally:
        await _drop_agency(tid)


async def test_post_marks_failed_when_enqueue_loses(queued, monkeypatch):
    """Redis down: `enqueue` fails soft, the row is left `failed` with a reason."""
    async def _enqueue(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(job_intelligence, "enqueue", _enqueue)
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert res.status_code == 202, res.text
            assert res.json()["state"] == "failed"
            assert res.json()["failure_reason"]
    finally:
        await _drop_agency(tid)


async def test_rerun_resets_a_finished_row_to_pending(queued):
    """A re-run resets the row rather than creating a second one."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            first = await c.post(f"/api/opportunities/{oid}/intelligence")
            row_id = first.json()["id"]

            # Simulate the worker finishing it.
            async with AdminSessionLocal() as s:
                await s.execute(
                    text(
                        "UPDATE job_intelligence SET state = 'done',"
                        " understanding = '{\"role\":\"x\"}'::jsonb"
                        " WHERE id = :i"
                    ),
                    {"i": row_id},
                )
                await s.commit()

            second = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert second.json()["id"] == row_id  # same row
            assert second.json()["state"] == "pending"  # reset
    finally:
        await _drop_agency(tid)


async def test_another_agencys_job_order_is_404(queued):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
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
