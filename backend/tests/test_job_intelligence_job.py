"""The run_job_intelligence arq job — claim, run, store, recover.

Mirrors `test_sourcing_job.py`: seeds a `pending` row, runs the job with the
engine's `analyze` replaced by a fake, and asserts the row reaches `done` with
the right content. A second assertion covers the claim guard — a row already
`done` is left alone, not re-run.

No test here reaches a model.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.job_intelligence import JobIntelligence
from app.services.job_intelligence.engine import JobIntelligenceOutcome, JobIntelligenceStats
from app.services.job_intelligence.schema import (
    CandidatePersona,
    JDUnderstanding,
    JobIntelligenceResult,
    SearchPlan,
)
from app.services.llm.client import LLMInvalidJSON
from app.workers.job_intelligence_jobs import run_job_intelligence
from tests.conftest import AdminSessionLocal, cleanup_tenant


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, a user, a job order, and a pending analysis row for it."""
    tid = uuid.uuid4()
    uid = uuid.uuid4()
    mailbox_id = uuid.uuid4()
    message_id = uuid.uuid4()
    oid = uuid.uuid4()
    row_id = uuid.uuid4()
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
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, retention_months) VALUES (:i, :t, :u, :m, 'inbox', 'user', 24)"
            ),
            {"i": mailbox_id, "t": tid, "u": uid, "m": f"oid-{mailbox_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " internet_message_id, subject) VALUES (:i, :t, :m, :g, :g, 'x')"
            ),
            {"i": message_id, "t": tid, "m": mailbox_id, "g": message_id.hex},
        )
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_raw, job_description, review_status, quality_state)"
                " VALUES (:i, :t, :e, 'Logistics Manager', 'Run the warehouse.',"
                " 'ready', 'likely')"
            ),
            {"i": oid, "t": tid, "e": message_id},
        )
        await s.execute(
            text(
                "INSERT INTO job_intelligence (id, tenant_id, opportunity_id, state, attempts)"
                " VALUES (:i, :t, :o, 'pending', 0)"
            ),
            {"i": row_id, "t": tid, "o": oid},
        )
        await s.commit()
    return tid, uid, oid, row_id


async def _drop(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in ("job_intelligence", "opportunities", "email_messages", "mailboxes"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.commit()
    await cleanup_tenant(tid)


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


async def test_job_runs_and_marks_done(monkeypatch):
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_MAX_ATTEMPTS", 3)
    fake = AsyncMock(return_value=_outcome())
    monkeypatch.setattr("app.workers.job_intelligence_jobs.analyze", fake)

    tid, uid, oid, row_id = await _seed()
    try:
        await run_job_intelligence(
            ctx={},
            tenant_id=str(tid),
            opportunity_id=str(oid),
            row_id=str(row_id),
        )

        async with tenant_session(tid) as s:
            row = await s.get(JobIntelligence, row_id)
            assert row.state == "done"
            assert row.understanding["role"] == "Logistics Manager"
            assert row.search_plan["platform"] == "LinkedIn"
            assert row.failure_reason is None
            assert row.attempts == 1
            assert row.analysed_at is not None
    finally:
        await _drop(tid)


async def test_job_leaves_a_done_row_alone(monkeypatch):
    """A row already `done` is not re-run — replaying a finished job is a no-op."""
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_MAX_ATTEMPTS", 3)
    fake = AsyncMock(return_value=_outcome())
    monkeypatch.setattr("app.workers.job_intelligence_jobs.analyze", fake)

    tid, uid, oid, row_id = await _seed()
    try:
        # Mark it done first.
        async with AdminSessionLocal() as s:
            await s.execute(
                text(
                    "UPDATE job_intelligence SET state = 'done',"
                    " understanding = '{\"role\":\"old\"}'::jsonb WHERE id = :i"
                ),
                {"i": row_id},
            )
            await s.commit()

        await run_job_intelligence(
            ctx={},
            tenant_id=str(tid),
            opportunity_id=str(oid),
            row_id=str(row_id),
        )

        # The claim guard matched no row, so analyze was never called and the
        # stored result is unchanged.
        assert fake.call_count == 0
        async with tenant_session(tid) as s:
            row = await s.get(JobIntelligence, row_id)
            assert row.understanding["role"] == "old"
    finally:
        await _drop(tid)


async def test_job_marks_failed_on_model_error(monkeypatch):
    """A bad model answer fails the row with a sentence, not an exception."""
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_MAX_ATTEMPTS", 3)
    fake = AsyncMock(side_effect=LLMInvalidJSON("bad"))
    monkeypatch.setattr("app.workers.job_intelligence_jobs.analyze", fake)

    tid, uid, oid, row_id = await _seed()
    try:
        await run_job_intelligence(
            ctx={},
            tenant_id=str(tid),
            opportunity_id=str(oid),
            row_id=str(row_id),
        )

        async with tenant_session(tid) as s:
            row = await s.get(JobIntelligence, row_id)
            assert row.state == "failed"
            assert row.failure_reason
            assert row.understanding is None
    finally:
        await _drop(tid)


async def test_job_gives_up_after_max_attempts(monkeypatch):
    """A row that has exhausted attempts is failed without running the pipeline."""
    monkeypatch.setattr(settings, "JOB_INTELLIGENCE_MAX_ATTEMPTS", 2)
    fake = AsyncMock(return_value=_outcome())
    monkeypatch.setattr("app.workers.job_intelligence_jobs.analyze", fake)

    tid, uid, oid, row_id = await _seed()
    try:
        # Pre-set attempts to the ceiling so the claim bumps it over.
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE job_intelligence SET attempts = 2 WHERE id = :i"),
                {"i": row_id},
            )
            await s.commit()

        await run_job_intelligence(
            ctx={},
            tenant_id=str(tid),
            opportunity_id=str(oid),
            row_id=str(row_id),
        )

        # The pipeline never ran, and the row is terminal.
        assert fake.call_count == 0
        async with tenant_session(tid) as s:
            row = await s.get(JobIntelligence, row_id)
            assert row.state == "failed"
    finally:
        await _drop(tid)
