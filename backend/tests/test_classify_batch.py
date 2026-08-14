"""The batched relevance gate.

Batching is a cost decision, and its risk is that a batch becomes a shared
fate: one malformed element taking the other nineteen emails with it, or —
worse — an email quietly vanishing from the response and therefore from the
pipeline. Most of what is asserted here is that neither happens.

The other half is the claim. The sweep and the job must not both be able to
send the same email to the model, and a worker killed mid-batch must leave its
rows somewhere `rescan_stuck` looks.

allow-hardcode: the SQL and the model responses below are test fixtures.
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.services.ingest import classify as classify_module
from app.services.ingest.classify import classify_many
from app.services.llm.client import FakeLLM, LLMInvalidJSON, complete_json
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs, tasks


@pytest.fixture(autouse=True)
def _configured_gate(monkeypatch):
    """Configuration of this test's own, never the repo `.env`.

    These passed locally and failed in CI once already, for exactly this
    reason: a test that depends on ambient configuration is testing the
    configuration. No model is ever called — the LLM is faked throughout.
    """
    monkeypatch.setattr(settings, "CLASSIFIER_MODEL", "test/classifier")
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://gate.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CLASSIFIER_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "CLASSIFIER_CHARS_PER_EMAIL", 50)
    monkeypatch.setattr(settings, "CLASSIFY_SWEEP_LIMIT", 100)


def _verdicts(*entries) -> dict:
    return {"verdicts": list(entries)}


# --- the batch call ---------------------------------------------------------


async def test_one_call_answers_the_whole_batch():
    """The entire point: N emails, one set of instructions, one round trip."""
    llm = FakeLLM(
        _verdicts(
            {"index": 0, "is_job_order": True, "reason": "a vacancy"},
            {"index": 1, "is_job_order": False, "reason": "an invoice"},
        )
    )

    results = await classify_many(["We need a QA Executive", "Invoice 4432"], llm=llm)

    assert [r.status for r in results] == ["recruitment", "non_recruitment"]
    assert len(llm.prompts) == 1


async def test_every_email_reaches_the_prompt_with_its_index():
    """A verdict is only usable if it can be traced back to one email."""
    llm = FakeLLM(_verdicts({"index": 0, "is_job_order": True, "reason": "r"}))

    await classify_many(["QA Executive wanted"], llm=llm)

    assert "index=0" in llm.prompts[0]
    assert "QA Executive wanted" in llm.prompts[0]


async def test_each_email_is_truncated_to_the_configured_length():
    """The gate answers a yes/no the opening of a message settles. Sending
    whole bodies would multiply the cost of the cheap stage for no better
    answer."""
    llm = FakeLLM(_verdicts({"index": 0, "is_job_order": True, "reason": "r"}))

    await classify_many(["x" * 500], llm=llm)

    assert "x" * settings.CLASSIFIER_CHARS_PER_EMAIL in llm.prompts[0]
    assert "x" * (settings.CLASSIFIER_CHARS_PER_EMAIL + 1) not in llm.prompts[0]


async def test_the_gate_calls_its_own_provider_not_the_router():
    """A model id or a URL in source is a defect (Global Constraints), and the
    gate deliberately does not go through the extraction router."""
    seen = {}

    async def spy(prompt, *, model, schema, **kwargs):
        seen.update(kwargs, model=model, schema=schema)
        return await FakeLLM(
            _verdicts({"index": 0, "is_job_order": True, "reason": "r"})
        )(prompt, model=model, schema=schema)

    await classify_many(["anything"], llm=spy)

    assert seen["model"] == settings.CLASSIFIER_MODEL
    assert seen["base_url"] == settings.LLM_PROVIDER_BASE_URL
    assert seen["api_key"] == settings.LLM_PROVIDER_API_KEY
    assert seen["schema"] is None, "this provider is asked for a bare JSON object"
    assert seen["extra_body"]["reasoning_effort"] == settings.CLASSIFIER_REASONING_EFFORT
    assert seen["extra_body"]["max_tokens"] == settings.CLASSIFIER_MAX_TOKENS


# --- one bad element must not poison the batch ------------------------------


async def test_an_email_the_model_omitted_falls_open_on_its_own():
    """The failure this whole file exists for: a silently dropped job order is
    a vacancy the recruiter never sees and never knows to look for."""
    llm = FakeLLM(_verdicts({"index": 0, "is_job_order": False, "reason": "invoice"}))

    results = await classify_many(["Invoice 4432", "We need a QA Executive"], llm=llm)

    assert [r.status for r in results] == ["non_recruitment", "uncertain"]


@pytest.mark.parametrize(
    "entry",
    [
        {"index": 1, "is_job_order": "yes", "reason": "not a boolean"},
        {"index": 1, "reason": "no verdict field at all"},
        {"index": "one", "is_job_order": False, "reason": "index is not an int"},
        {"is_job_order": False, "reason": "no index at all"},
        "not even an object",
    ],
)
async def test_an_unusable_verdict_costs_only_its_own_email(entry):
    """A missing key read as False would be the one wrong answer that discards
    the email, and a guessed index would attach a verdict to the wrong one."""
    llm = FakeLLM(
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}, entry)
    )

    results = await classify_many(["a vacancy", "something odd"], llm=llm)

    assert results[0].status == "recruitment", "a good element keeps its answer"
    assert results[1].status == "uncertain"


async def test_a_failed_call_fails_the_whole_batch_open():
    """Failing closed loses job orders; failing open costs a fraction of a cent
    per email."""

    async def broken(prompt, **kwargs):
        raise LLMInvalidJSON("the model returned no content")

    results = await classify_many(["a", "b", "c"], llm=broken)

    assert [r.status for r in results] == ["uncertain"] * 3


async def test_a_response_that_is_not_a_verdict_list_loses_no_email():
    llm = FakeLLM({"answer": "sure!"})

    results = await classify_many(["a", "b"], llm=llm)

    assert len(results) == 2
    assert all(r.status == "uncertain" for r in results)


async def test_an_empty_batch_makes_no_call():
    llm = FakeLLM()  # no queued responses: a call would raise

    assert await classify_many([], llm=llm) == []


# --- the job ----------------------------------------------------------------


@pytest.fixture
async def batch(admin_session):
    """One tenant, one mailbox, two rows at `classifying` — as claimed."""
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"b-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    row_ids = []
    for n in (1, 2):
        row_id = uuid.uuid4()
        row_ids.append(row_id)
        await admin_session.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
                "  body_html_r2_key, processing_status, retention_until)"
                " VALUES (:id, :tenant, :mailbox, :gid, :subject, 'evelyn@example.com',"
                "  :key, 'classifying', now() + interval '720 days')"
            ),
            {
                "id": row_id,
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "gid": f"MSG-{n}",
                "subject": f"Subject {n}",
                "key": body_key(tenant_id, mailbox_id, f"MSG-{n}", "html"),
            },
        )
    await admin_session.commit()
    # Sorted, because the job reads its rows ordered by id and the assertions
    # below pair a verdict with a row by position.
    yield tenant_id, mailbox_id, sorted(row_ids)
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _wire(monkeypatch, *responses):
    """Fake R2, fake model, captured queue. Nothing reaches the network."""
    store = InMemoryBodyStore()
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    llm = FakeLLM(*responses)
    monkeypatch.setattr(jobs, "body_store", lambda: store)
    monkeypatch.setattr(jobs, "enqueue", _enqueue)
    monkeypatch.setattr(classify_module, "complete_json", llm)
    return store, queued, llm


async def _bodies(store, tenant_id, mailbox_id):
    for n in (1, 2):
        await store.put(
            body_key(tenant_id, mailbox_id, f"MSG-{n}", "html"), f"<p>Body {n}</p>"
        )


async def _row(tenant_id, row_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT * FROM email_messages WHERE id = :id"), {"id": row_id}
            )
        ).one()


async def test_the_job_writes_a_verdict_for_every_email_in_one_call(
    monkeypatch, batch
):
    tenant_id, mailbox_id, row_ids = batch
    store, queued, llm = _wire(
        monkeypatch,
        _verdicts(
            {"index": 0, "is_job_order": True, "reason": "a vacancy"},
            {"index": 1, "is_job_order": False, "reason": "an invoice"},
        ),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(i) for i in row_ids]
    )

    first, second = [await _row(tenant_id, i) for i in row_ids]
    assert first.classification_status == "recruitment"
    assert first.processing_status == "classified"
    assert second.classification_status == "non_recruitment"
    assert second.processing_status == "skipped"
    assert len(llm.prompts) == 1, "two emails must cost one model call"
    assert [name for name, _ in queued] == ["extract_email"]
    assert queued[0][1]["email_message_id"] == str(row_ids[0])


async def test_the_gate_judges_the_text_the_extractor_will_quote(monkeypatch, batch):
    tenant_id, mailbox_id, row_ids = batch
    store, _, llm = _wire(
        monkeypatch, _verdicts({"index": 0, "is_job_order": True, "reason": "r"})
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(row_ids[0])]
    )

    prompt = llm.prompts[0]
    assert "<p>" not in prompt
    assert "SENDER: evelyn@example.com" in prompt


async def test_an_email_the_model_omitted_still_reaches_extraction(monkeypatch, batch):
    """The batch's blast radius, measured: the omitted email is not dropped and
    not marked `skipped` — it goes on to extraction like any uncertain one."""
    tenant_id, mailbox_id, row_ids = batch
    store, queued, _ = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": False, "reason": "an invoice"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(i) for i in row_ids]
    )

    second = await _row(tenant_id, row_ids[1])
    assert second.classification_status == "uncertain"
    assert [kw["email_message_id"] for _, kw in queued] == [str(row_ids[1])]


async def test_a_body_store_outage_leaves_the_batch_recoverable(monkeypatch, batch):
    """The rows must stay where `rescan_stuck` looks, not go terminal."""
    tenant_id, _, row_ids = batch
    _wire(monkeypatch, _verdicts({"index": 0, "is_job_order": True, "reason": "r"}))

    class Broken:
        async def get(self, key):
            raise RuntimeError("bucket is gone")

    monkeypatch.setattr(jobs, "body_store", Broken)

    with pytest.raises(RuntimeError):
        await jobs.classify_batch(
            {}, tenant_id=str(tenant_id), email_message_ids=[str(i) for i in row_ids]
        )

    assert (await _row(tenant_id, row_ids[0])).processing_status == "classifying"


async def test_a_verdicted_member_is_never_re_billed_at_the_gate(monkeypatch, batch):
    """A replayed batch must not pay for the members it already answered.

    Both rows carry a verdict and sit at `classifying`, which is what the live
    rows looked like. One model call for them is one call too many.
    """
    tenant_id, _, row_ids = batch
    _, queued, llm = _wire(monkeypatch)  # no responses: a call would raise
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE email_messages SET classification_status = 'recruitment'"
                " WHERE id = ANY(:ids)"
            ),
            {"ids": list(row_ids)},
        )

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(i) for i in row_ids]
    )

    assert llm.prompts == []
    assert queued == []


async def test_a_row_belonging_to_another_tenant_is_not_touched(monkeypatch, batch):
    """RLS decides, and the job must not fabricate work when it decides no."""
    tenant_id, _, row_ids = batch
    _, queued, llm = _wire(monkeypatch)  # no responses: a call would raise

    await jobs.classify_batch(
        {}, tenant_id=str(uuid.uuid4()), email_message_ids=[str(i) for i in row_ids]
    )

    assert llm.prompts == []
    assert queued == []
    # `unknown` is the untouched state, not None: the column is NOT NULL with
    # server_default 'unknown', so no row is ever None and the original
    # assertion described a state the schema cannot produce. It failed for that
    # reason rather than for the one it was written to catch — which is worse
    # than a missing test, because a red result read as an environment quirk.
    assert (await _row(tenant_id, row_ids[0])).classification_status == "unknown"


async def test_an_unconfigured_gate_refuses_rather_than_guessing(monkeypatch):
    """The gate fails open, which is right — and dangerous when unconfigured:
    every email would be `uncertain`, all of them would be extracted, and the
    system would look like it worked with an indecisive model."""
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "")

    with pytest.raises(RuntimeError, match="no model configured"):
        await jobs.classify_batch(
            {}, tenant_id=str(uuid.uuid4()), email_message_ids=[str(uuid.uuid4())]
        )


# --- the sweep --------------------------------------------------------------


@pytest.fixture
def queued(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        calls.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)
    return calls


async def _fetched_rows(session, count: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"s-{tenant_id.hex[:8]}"},
    )
    await session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    ids = []
    for _n in range(count):
        row_id = uuid.uuid4()
        ids.append(row_id)
        await session.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
                "  retention_until)"
                " VALUES (:id, :tenant, :mailbox, :gid, 'fetched',"
                "  now() + interval '720 days')"
            ),
            {
                "id": row_id,
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "gid": f"SWEEP-{row_id.hex[:8]}",
            },
        )
    return tenant_id, ids


@pytest.fixture
async def swept(admin_session):
    """Two tenants with fetched mail, cleaned up afterwards."""
    a_tenant, a_ids = await _fetched_rows(admin_session, 3)
    b_tenant, b_ids = await _fetched_rows(admin_session, 1)
    await admin_session.commit()
    yield (a_tenant, a_ids), (b_tenant, b_ids)
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a_tenant, "b": b_tenant}
    )
    await admin_session.commit()


async def test_the_sweep_batches_by_tenant_and_by_size(swept, queued):
    """A batch spanning tenants would read none of the other tenant's rows
    under RLS — the correct outcome, and an invisible way to lose mail."""
    (a_tenant, a_ids), (b_tenant, b_ids) = swept

    batches = await tasks.classify_fetched()

    assert batches == 3, "three of tenant A at size 2, plus one of tenant B"
    assert {name for name, _ in queued} == {"classify_batch"}
    by_tenant: dict[str, list[str]] = {}
    for _, kw in queued:
        assert len(kw["email_message_ids"]) <= settings.CLASSIFIER_BATCH_SIZE
        by_tenant.setdefault(kw["tenant_id"], []).extend(kw["email_message_ids"])
    assert sorted(by_tenant[str(a_tenant)]) == sorted(str(i) for i in a_ids)
    assert by_tenant[str(b_tenant)] == [str(b_ids[0])]


async def test_the_claim_commits_so_a_second_sweep_finds_nothing(swept, queued):
    """Reading without claiming would hand the same emails to a second batch
    and bill for every one of them twice."""
    await tasks.classify_fetched()
    queued.clear()

    assert await tasks.classify_fetched() == 0
    assert queued == []


async def test_claimed_rows_sit_where_rescan_stuck_looks(swept, queued, admin_session):
    """A worker killed between the claim and the verdict must not strand mail."""
    (a_tenant, a_ids), _ = swept

    await tasks.classify_fetched()

    statuses = (
        await admin_session.execute(
            text("SELECT DISTINCT processing_status FROM email_messages"
                 " WHERE tenant_id = :t"),
            {"t": a_tenant},
        )
    ).scalars().all()
    assert statuses == ["classifying"]
    assert tasks.RESUME_JOB["classifying"] == "classify_email"


async def test_the_sweep_is_registered_with_the_supervisor():
    """The sweep is the only thing that starts classification now — unregistered,
    every fetched email would sit at `fetched` forever."""
    from app.workers.main import build_tasks

    assert "classify_fetched" in {t.name for t in build_tasks()}


async def test_the_batch_job_is_registered_with_the_worker():
    """The production failure this guards: arq logged 'function not found'
    while the producer reported success."""
    from app.workers.settings import WorkerSettings

    assert jobs.classify_batch in WorkerSettings.functions


# --- the client seam --------------------------------------------------------


def _transport(payload, status=200):
    return httpx.MockTransport(lambda r: httpx.Response(status, json=payload))


async def test_a_response_with_reasoning_and_no_content_is_rejected():
    """Observed against the live provider: at the default reasoning effort the
    model spends its whole budget thinking and returns a message with a
    `reasoning` key and no `content` key at all. Indexing would raise KeyError,
    which reads as a bug here rather than as the unusable answer it is — and
    only the latter is what the gate's fail-open path handles."""
    payload = {"choices": [{"message": {"reasoning": "hmm...", "role": "assistant"}}]}

    with pytest.raises(LLMInvalidJSON):
        await complete_json(
            "prompt",
            model="test/classifier",
            schema=None,
            base_url="https://gate.test/v1",
            transport=_transport(payload),
        )


async def test_the_provider_override_and_extra_params_are_actually_sent():
    """A base URL accepted and dropped would silently send the gate's traffic
    to the extraction router, on the extraction key."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    await complete_json(
        "prompt",
        model="test/classifier",
        schema=None,
        base_url="https://gate.test/v1",
        api_key="gate-key",
        extra_body={"reasoning_effort": "low"},
        transport=httpx.MockTransport(handler),
    )

    assert seen["url"].startswith("https://gate.test/v1")
    assert seen["auth"] == "Bearer gate-key"
    assert seen["body"]["reasoning_effort"] == "low"
    assert seen["body"]["response_format"] == {"type": "json_object"}
