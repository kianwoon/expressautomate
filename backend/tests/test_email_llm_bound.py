"""The email LLM rebill bound (migration c1v2n0000001).

One property carries the product here, and every test in this module fails
loudly if it stops holding:

**A row whose job deterministically crashes is re-billed a bounded number of
times, then parked `failed` — before any further model call.**

Before c1v2n0000001, `email_messages` was the one LLM-paying table with no
attempt bound: an unexpected exception in `extract_email` (its except clause
covered only `LLMInvalidJSON`) left the row at `extracting`, `rescan_stuck`
re-enqueued it every RESCAN_INTERVAL_SECONDS, and each pickup re-billed up to
three model calls — forever, until a human noticed. Every other LLM-paying
row already had this bound; see migration c1v2l0000001 for the CV version of
the same story.

The bound counts sweep-recovery pickups on `email_messages.llm_attempts`
(deliberately NOT `attempt_count`, which is fetch-scoped): a row resumed
while ALREADY in its working status is a crash-loop iteration by definition,
because forward progress never re-enters a status it holds.

No test here reaches a model. `extract` is monkeypatched with a bomb that
counts its own detonations — the assertion is that the bomb goes off at most
EMAIL_LLM_MAX_ATTEMPTS + 1 times (the last pickup is the refusal, which pays
nothing), never forever.

allow-hardcode: the SQL, ids and statuses below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs

SOURCE = "Finance officer at KLN Logistics. Salary up to $3500 per month."


@pytest.fixture(autouse=True)
def _configured_extraction(monkeypatch):
    """Every test gets its own models. Nothing here ever calls one."""
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_STRONG", "test/strong")
    monkeypatch.setattr(settings, "CLASSIFIER_MODEL", "test/fast")
    monkeypatch.setattr(settings, "EMAIL_LLM_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(jobs, "body_store", lambda: InMemoryBodyStore())


@pytest.fixture
async def email_row(admin_session):
    tenant_id, mailbox_id, row_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tenant_id, "s": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope,"
            " status, retention_months)"
            " VALUES (:i, :t, 'ms-user-1', 'inbox', 'folder', 'active', 24)"
        ),
        {"i": mailbox_id, "t": tenant_id},
    )
    key = body_key(tenant_id, mailbox_id, "MSG-1", "html")
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " received_datetime, processing_status, source_state, classification_status,"
            " body_html_r2_key, subject, sender_email)"
            " VALUES (:i, :t, :m, 'MSG-1', now(), :status, 'present',"
            " 'recruitment', :key, 'Finance officer', 'evelyn@example.com')"
        ),
        {"i": row_id, "t": tenant_id, "m": mailbox_id, "key": key, "status": "classified"},
    )
    await admin_session.commit()
    await InMemoryBodyStore().put_bytes(key, f"<p>{SOURCE}</p>".encode(), "text/html")
    yield tenant_id, mailbox_id, row_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id}
    )
    await admin_session.commit()


async def _status(admin_session, row_id) -> tuple[str, int]:
    """The row's current processing status and spent LLM attempts."""
    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, llm_attempts FROM email_messages"
                " WHERE id = :i"
            ),
            {"i": row_id},
        )
    ).one()
    return row.processing_status, row.llm_attempts


async def test_a_crashing_extraction_is_parked_after_the_bound(
    monkeypatch, admin_session, email_row
):
    """The infinite rebill loop, closed.

    An extraction that raises an unexpected exception (not LLMInvalidJSON —
    something the job does not catch) used to leave the row at `extracting`
    forever, with `rescan_stuck` paying for a fresh attempt every sweep. Now
    the recovery pickups spend `llm_attempts` BEFORE the model call, and past
    the ceiling the row is parked `failed` — terminal, so the sweep stops
    seeing it.
    """
    tenant_id, mailbox_id, row_id = email_row
    detonations = 0

    async def bomb(source):
        nonlocal detonations
        detonations += 1
        raise RuntimeError("unexpected: infrastructure, not a model answer")

    monkeypatch.setattr("app.services.ingest.extract.extract", bomb)

    # The healthy first pickup: `classified` row, claims, pays once, crashes.
    # The RuntimeError escapes the job exactly as it would escape arq's — the
    # row is left at `extracting`, which is what `rescan_stuck` finds.
    with pytest.raises(RuntimeError):
        await jobs.extract_email(
            ctx=None,
            email_message_id=str(row_id),
            tenant_id=str(tenant_id),
            mailbox_id=str(mailbox_id),
        )
    assert detonations == 1
    status, spent = await _status(admin_session, row_id)
    assert status == "extracting"  # crashed mid-call; the sweep will find it
    assert spent == 0  # forward progress spends nothing

    # The sweep re-enqueues; each recovery pickup spends one attempt. The
    # bomb still crashes, but the spend is counted before the call — until
    # the ceiling, where the pickup refuses (returns cleanly, having parked
    # the row `failed`) without paying the bomb at all.
    for _ in range(settings.EMAIL_LLM_MAX_ATTEMPTS + 2):
        try:
            await jobs.extract_email(
                ctx=None,
                email_message_id=str(row_id),
                tenant_id=str(tenant_id),
                mailbox_id=str(mailbox_id),
            )
        except RuntimeError:
            pass  # a paid pickup that crashed, as arq would see it

    # The bound held: the first (legitimate) pickup plus at most MAX
    # recovery pickups are paid, then the row is terminal — a thousand more
    # sweeps would pay nothing.
    assert detonations == settings.EMAIL_LLM_MAX_ATTEMPTS + 1
    status, spent = await _status(admin_session, row_id)
    assert status == "failed"
    assert spent == settings.EMAIL_LLM_MAX_ATTEMPTS + 1


async def test_the_healthy_path_spends_nothing(
    monkeypatch, admin_session, email_row
):
    """Forward progress keeps its whole budget.

    The first classify (fetched→classifying) and first extract
    (classified→extracting) must not spend `llm_attempts` — only a row
    resumed while ALREADY in its working status does. A ceiling that counted
    healthy emails would fail every long reply chain after three extractions.
    """
    tenant_id, mailbox_id, row_id = email_row

    async def answer(source):
        from app.services.ingest.extract import ExtractionResponse
        from app.services.llm.client import LLMResult

        return (
            ExtractionResponse.model_validate({"jobs": []}),
            LLMResult(data={"jobs": []}, model="test/fast"),
        )

    monkeypatch.setattr("app.services.ingest.extract.extract", answer)

    # The healthy first pickup pays the model, succeeds, and spends nothing.
    await jobs.extract_email(
        ctx=None,
        email_message_id=str(row_id),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )
    status, spent = await _status(admin_session, row_id)
    assert status == "no_opportunity"  # zero vacancies — a real, finished run
    assert spent == 0


async def test_a_fresh_classify_spends_nothing_but_a_recovery_pays(
    monkeypatch, admin_session, email_row
):
    """The gate's bound, on the same rule as the extractor's.

    A row that arrives `fetched` is first-time work; a row the sweep hands
    back still `classifying` is a crash-loop iteration and pays.
    """
    tenant_id, mailbox_id, row_id = email_row
    # An unanswered row still at `classifying`: the crash-loop shape. The
    # verdict guard returns an already-answered row before the gate, so this
    # row must be `unknown` to reach the spend at all.
    await admin_session.execute(
        text(
            "UPDATE email_messages SET processing_status = 'classifying',"
            " classification_status = 'unknown' WHERE id = :i"
        ),
        {"i": row_id},
    )
    await admin_session.commit()

    async def verdict(body):
        from app.services.ingest.classify import Classification

        return Classification(
            status="recruitment",
            reason="a role to hire for",
            model="test/fast",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
        )

    monkeypatch.setattr("app.services.ingest.classify.classify", verdict)

    # Recovery pickup of a `classifying` row: spends one before the gate.
    await jobs.classify_email(
        ctx=None,
        email_message_id=str(row_id),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )
    _, spent = await _status(admin_session, row_id)
    assert spent == 1


async def test_a_successful_replay_hands_the_budget_back(
    monkeypatch, admin_session, email_row
):
    """Healthy replays never burn the lifetime budget (BUG 2 of the review).

    The replay sweep exists to refresh every email on each prompt upgrade;
    if each healthy replay spent `llm_attempts`, an email surviving four
    upgrades would be parked `failed` on the fourth — degrading the upgrade
    contract for exactly the long-lived rows it exists to refresh. Success
    resets the counter, so only *failed* pickups accumulate: a row whose
    replays keep crashing still reaches the ceiling and parks.
    """
    tenant_id, mailbox_id, row_id = email_row
    # The replay claim resolver's shape: the row arrives at `replaying`.
    await admin_session.execute(
        text(
            "UPDATE email_messages SET processing_status = 'replaying'"
            " WHERE id = :i"
        ),
        {"i": row_id},
    )
    await admin_session.commit()

    async def answer(source):
        from app.services.ingest.extract import ExtractionResponse
        from app.services.llm.client import LLMResult

        return (
            ExtractionResponse.model_validate({"jobs": []}),
            LLMResult(data={"jobs": []}, model="test/fast"),
        )

    monkeypatch.setattr("app.services.ingest.extract.extract", answer)
    # Stub persist: this test is about the budget, not the opportunity rows.
    async def fake_persist(*args, **kwargs):
        return []

    monkeypatch.setattr("app.services.ingest.persist.persist", fake_persist)

    # Five healthy replays (five prompt upgrades' worth): every one spends,
    # succeeds, and hands the budget back.
    for _ in range(5):
        await jobs.replay_email(
            ctx=None,
            email_message_id=str(row_id),
            tenant_id=str(tenant_id),
            mailbox_id=str(mailbox_id),
        )
    _, spent = await _status(admin_session, row_id)
    assert spent == 0  # success always resets


async def test_crashing_replays_park_after_the_bound(
    monkeypatch, admin_session, email_row
):
    """A row whose replays keep crashing is bounded, not re-billed forever."""
    tenant_id, mailbox_id, row_id = email_row
    await admin_session.execute(
        text(
            "UPDATE email_messages SET processing_status = 'replaying'"
            " WHERE id = :i"
        ),
        {"i": row_id},
    )
    await admin_session.commit()

    detonations = 0

    async def bomb(source):
        nonlocal detonations
        detonations += 1
        raise RuntimeError("unexpected")

    monkeypatch.setattr("app.services.ingest.extract.extract", bomb)

    for _ in range(settings.EMAIL_LLM_MAX_ATTEMPTS + 2):
        await admin_session.execute(
            text(
                "UPDATE email_messages SET processing_status = 'replaying'"
                " WHERE id = :i"
            ),
            {"i": row_id},
        )
        await admin_session.commit()
        try:
            await jobs.replay_email(
                ctx=None,
                email_message_id=str(row_id),
                tenant_id=str(tenant_id),
                mailbox_id=str(mailbox_id),
            )
        except RuntimeError:
            pass
    # No free first pickup on the replay path (the claim resolver pre-claims
    # every one), so the budget pays exactly MAX model calls then refuses —
    # the row is terminal, and a thousand more sweep re-claims would pay
    # nothing (the status guard turns them away before any spend).
    assert detonations == settings.EMAIL_LLM_MAX_ATTEMPTS
    status, _ = await _status(admin_session, row_id)
    assert status == "failed"
