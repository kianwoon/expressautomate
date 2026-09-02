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

            # The right job was enqueued with the row id and tenant — and on
            # the interactive queue, so a background replay backlog can never
            # starve a recruiter's click.
            assert len(queued) == 1
            name, kwargs = queued[0]
            assert name == "run_job_intelligence"
            assert kwargs["opportunity_id"] == str(oid)
            assert kwargs["tenant_id"] == str(tid)
            assert "row_id" in kwargs
            assert kwargs["queue_name"] == "arq:interactive"
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


async def test_rerun_resets_attempts_so_a_failed_row_can_run_again(queued):
    """The attempts counter must not accumulate across re-runs.

    A failed analysis (attempts spent, then a re-run) would otherwise hit the
    job's conditional claim with `attempts > JOB_INTELLIGENCE_MAX_ATTEMPTS`
    and fail instantly with "attempts exhausted" before ever calling the
    model. This pins the bug that made a re-run of a failed job order die
    with no error logged: the POST must reset attempts along with state.
    """
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            first = await c.post(f"/api/opportunities/{oid}/intelligence")
            row_id = first.json()["id"]

            # Simulate a failed run: attempts spent at the claim, row failed.
            async with AdminSessionLocal() as s:
                await s.execute(
                    text(
                        "UPDATE job_intelligence SET state = 'failed',"
                        " attempts = 4, failure_reason = 'model failed'"
                        " WHERE id = :i"
                    ),
                    {"i": row_id},
                )
                await s.commit()

            # Re-run: the POST must reset attempts to 0 along with state.
            second = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert second.json()["id"] == row_id
            assert second.json()["state"] == "pending"

            async with AdminSessionLocal() as s:
                got = (
                    await s.execute(
                        text("SELECT attempts FROM job_intelligence WHERE id = :i"),
                        {"i": row_id},
                    )
                ).scalar_one()
            assert got == 0
    finally:
        await _drop_agency(tid)


async def test_allow_thin_is_stored_and_reported_on_a_failed_row(queued):
    """The "Run anyway" flag travels POST → row → GET, and a failed row over a
    thin order carries `thin: true` so the panel offers the override.

    The frontend must never string-match `failure_reason` copy to detect the
    thin case — this pins the machine flag it reads instead.
    """
    from app.core.config import settings as cfg

    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        # Make the order thin: title only.
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE opportunities SET job_description = '' WHERE id = :i"),
                {"i": oid},
            )
            await s.commit()
        monkeypatch_quota = cfg.INTELLIGENCE_DAILY_QUOTA
        assert monkeypatch_quota  # quota is set; this test spends one run

        async with _http(tid, uid) as c:
            # A plain run: pending, and no thin marker yet (not failed).
            first = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert first.status_code == 202
            row_id = first.json()["id"]

            # Simulate the thin-context guard failing it.
            async with AdminSessionLocal() as s:
                await s.execute(
                    text(
                        "UPDATE job_intelligence SET state = 'failed',"
                        " failure_reason = 'thin order' WHERE id = :i"
                    ),
                    {"i": row_id},
                )
                await s.commit()

            got = await c.get(f"/api/opportunities/{oid}/intelligence")
            assert got.json()["state"] == "failed"
            assert got.json()["thin"] is True

            # Run anyway: the flag is stored on the row for the worker.
            second = await c.post(
                f"/api/opportunities/{oid}/intelligence", params={"allow_thin": "true"}
            )
            assert second.status_code == 202
            async with AdminSessionLocal() as s:
                stored = (
                    await s.execute(
                        text("SELECT allow_thin FROM job_intelligence WHERE id = :i"),
                        {"i": row_id},
                    )
                ).scalar_one()
            assert stored is True

            # A plain re-run clears the override — the guard is back.
            third = await c.post(f"/api/opportunities/{oid}/intelligence")
            assert third.status_code == 202
            async with AdminSessionLocal() as s:
                stored = (
                    await s.execute(
                        text("SELECT allow_thin FROM job_intelligence WHERE id = :i"),
                        {"i": row_id},
                    )
                ).scalar_one()
            assert stored is False
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


async def test_re_runs_count_against_the_daily_quota(queued, monkeypatch):
    """The quota counts RUNS, not rows — the upsert re-run hole.

    A re-run is an UPDATE (created_at never moves), so a `created_at`-based
    count would sit at 1 while a looping client spent 4-5 model calls per
    POST. `last_queued_at` is stamped on every POST in both upsert branches;
    the N+1th run of the SAME opportunity is refused with 429.
    """
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "INTELLIGENCE_DAILY_QUOTA", 3)
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            statuses = [
                (await c.post(f"/api/opportunities/{oid}/intelligence")).status_code
                for _ in range(5)
            ]
        # The first three runs pass (202), every run after the quota is 429 —
        # same row, same opportunity, real re-spend each time.
        assert statuses == [202, 202, 202, 429, 429]
    finally:
        await _drop_agency(tid)


async def test_the_quota_is_per_agency(queued, monkeypatch):
    """One agency hitting its ceiling does not cap another's spend."""
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "INTELLIGENCE_DAILY_QUOTA", 2)
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    other_tid, other_uid = await _seed_agency()
    try:
        async with _http(tid, uid) as c:
            for _ in range(3):
                await c.post(f"/api/opportunities/{oid}/intelligence")
            refused = await c.post(f"/api/opportunities/{oid}/intelligence")
        other_oid = await _opportunity(other_tid, other_uid)
        async with _http(other_tid, other_uid) as c:
            fresh = await c.post(f"/api/opportunities/{other_oid}/intelligence")
        assert refused.status_code == 429
        assert fresh.status_code == 202
    finally:
        await _drop_agency(tid)
        await _drop_agency(other_tid)
