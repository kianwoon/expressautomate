"""The run_candidate_intelligence arq job — claim, run, store, recover, retry.

Mirrors `test_job_intelligence_job.py`: seeds a `pending` row, runs the job
with the engine's `analyze_candidate` replaced by a fake, and asserts the row
reaches `done` with the right content. The retry behaviour is what this file
exists to pin: an empty model response (`LLMNoContent`) is not an answer, so
the job re-asks a bounded number of times before failing; an unparseable
answer (`LLMInvalidJSON`) gets the same bounded retry since GLM's coding-plan
envelope is inconsistent — but never loops beyond the budget.

No test here reaches a model or R2 — `_candidate_text` is faked like
`analyze_candidate` is.

allow-hardcode: the fixtures below are test content, not an oracle.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.candidate_intelligence import CandidateIntelligence
from app.services.candidate_intelligence.engine import (
    CandidateIntelligenceOutcome,
    CandidateIntelligenceStats,
)
from app.services.candidate_intelligence.schema import (
    CandidateAssessment,
    CandidateIntelligenceResult,
    WorkAssessment,
)
from app.services.llm.client import LLMInvalidJSON, LLMNoContent
from app.workers.candidate_intelligence_jobs import run_candidate_intelligence
from tests.conftest import AdminSessionLocal, cleanup_tenant

_CV = "Underwriting experience across personal and commercial lines."


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, a candidate, and a pending analysis row for the candidate."""
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    row_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name) "
                "VALUES (:i, :t, 'Siti Rahman')"
            ),
            {"i": cid, "t": tid},
        )
        await s.execute(
            text(
                "INSERT INTO candidate_intelligence (id, tenant_id, candidate_id,"
                " state, attempts) VALUES (:i, :t, :c, 'pending', 0)"
            ),
            {"i": row_id, "t": tid, "c": cid},
        )
        await s.commit()
    return tid, cid, row_id


async def _drop(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in ("candidate_intelligence", "candidates"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.commit()
    await cleanup_tenant(tid)


def _outcome():
    return CandidateIntelligenceOutcome(
        result=CandidateIntelligenceResult(
            work=WorkAssessment(roles=[], education=[]),
            assessment=CandidateAssessment(
                headline="A routine operator with a senior title",
                work_level="operational",
            ),
        ),
        stats=CandidateIntelligenceStats(
            model="test/fast", prompt_tokens=100, completion_tokens=200, latency_ms=3000
        ),
    )


def _wire(monkeypatch, fake) -> None:
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("app.workers.candidate_intelligence_jobs.analyze_candidate", fake)
    monkeypatch.setattr(
        "app.workers.candidate_intelligence_jobs._candidate_text",
        AsyncMock(return_value=_CV),
    )


async def test_job_runs_and_marks_done(monkeypatch):
    fake = AsyncMock(return_value=_outcome())
    _wire(monkeypatch, fake)

    tid, cid, row_id = await _seed()
    try:
        await run_candidate_intelligence(
            ctx={}, tenant_id=str(tid), candidate_id=str(cid), row_id=str(row_id)
        )

        async with tenant_session(tid) as s:
            row = await s.get(CandidateIntelligence, row_id)
            assert row.state == "done"
            assert row.assessment["headline"] == "A routine operator with a senior title"
            assert row.work["roles"] == []
            assert row.failure_reason is None
            assert row.attempts == 1
            assert row.model_name == "test/fast"
            assert row.analysed_at is not None
    finally:
        await _drop(tid)


async def test_job_marks_failed_on_model_error(monkeypatch):
    """A real-but-bad answer fails the row after the retry budget is spent.

    GLM's coding-plan envelope is inconsistent, so an unparseable answer is
    re-asked once (the model's envelope choice and reasoning trace change
    each call) — but a model that keeps answering in the wrong shape fails
    the row rather than looping forever.
    """
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRIES", 1)
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRY_DELAY_SECONDS", 0)
    fake = AsyncMock(side_effect=LLMInvalidJSON("bad"))
    _wire(monkeypatch, fake)

    tid, cid, row_id = await _seed()
    try:
        await run_candidate_intelligence(
            ctx={}, tenant_id=str(tid), candidate_id=str(cid), row_id=str(row_id)
        )

        async with tenant_session(tid) as s:
            row = await s.get(CandidateIntelligence, row_id)
            assert row.state == "failed"
            assert row.failure_reason
            assert row.work is None
        assert fake.call_count == 2, "one retry for an unparseable GLM answer, then fail"
    finally:
        await _drop(tid)


async def test_job_retries_no_content_then_succeeds(monkeypatch):
    """An empty response is not an answer: the job re-asks once and succeeds."""
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRIES", 1)
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRY_DELAY_SECONDS", 0)
    fake = AsyncMock(side_effect=[LLMNoContent("the model returned no content"), _outcome()])
    _wire(monkeypatch, fake)

    tid, cid, row_id = await _seed()
    try:
        await run_candidate_intelligence(
            ctx={}, tenant_id=str(tid), candidate_id=str(cid), row_id=str(row_id)
        )

        async with tenant_session(tid) as s:
            row = await s.get(CandidateIntelligence, row_id)
            assert row.state == "done"
            assert row.assessment["headline"] == "A routine operator with a senior title"
        assert fake.call_count == 2, "one no-content retry, then success"
    finally:
        await _drop(tid)


async def test_job_fails_after_no_content_retries_exhausted(monkeypatch):
    """A model that never answers exhausts the retry budget and fails the row."""
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRIES", 2)
    monkeypatch.setattr(settings, "CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRY_DELAY_SECONDS", 0)
    fake = AsyncMock(side_effect=LLMNoContent("the model returned no content"))
    _wire(monkeypatch, fake)

    tid, cid, row_id = await _seed()
    try:
        await run_candidate_intelligence(
            ctx={}, tenant_id=str(tid), candidate_id=str(cid), row_id=str(row_id)
        )

        async with tenant_session(tid) as s:
            row = await s.get(CandidateIntelligence, row_id)
            assert row.state == "failed"
            assert row.failure_reason
        assert fake.call_count == 3, "2 configured retries + the original attempt"
    finally:
        await _drop(tid)


async def test_job_gives_up_after_max_attempts(monkeypatch):
    """A row that has exhausted attempts is failed without running the pipeline."""
    fake = AsyncMock(return_value=_outcome())
    _wire(monkeypatch, fake)

    tid, cid, row_id = await _seed()
    try:
        # Pre-set attempts to the ceiling so the claim bumps it over.
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE candidate_intelligence SET attempts = 3 WHERE id = :i"),
                {"i": row_id},
            )
            await s.commit()

        await run_candidate_intelligence(
            ctx={}, tenant_id=str(tid), candidate_id=str(cid), row_id=str(row_id)
        )

        assert fake.call_count == 0
        async with tenant_session(tid) as s:
            row = await s.get(CandidateIntelligence, row_id)
            assert row.state == "failed"
    finally:
        await _drop(tid)
