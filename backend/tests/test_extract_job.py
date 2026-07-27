"""Extraction, persistence, and the job that joins them (plan Task 7).

Three properties carry the product here, and each has a test that fails loudly
if it stops holding:

- A run that found nothing still leaves a record. The zero-vacancy run is the
  one worth inspecting, and an absent row looks exactly like a run that never
  happened.
- Replay never touches a human correction. A prompt upgrade is worthless if
  running it undoes a year of recruiters' fixes.
- The strong model is reached for only when the fast one demonstrably fell
  short — never as a plain retry, which at temperature zero buys the same
  answer twice.

No test here reaches a model. `complete_json` is faked everywhere, and the
autouse fixture below supplies the settings rather than trusting the ambient
environment — CI has no `.env`, and a test that depends on one is testing the
configuration.

allow-hardcode: the SQL, model ids and model responses below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.ingest import extract as extract_module
from app.services.ingest.extract import extract
from app.services.ingest.persist import persist
from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMInvalidJSON, LLMResult
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs

SOURCE = "Finance officer at KLN Logistics. Salary up to $3500 per month."


@pytest.fixture(autouse=True)
def _configured_extraction(monkeypatch):
    """Every test gets its own models. Nothing here ever calls one."""
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_STRONG", "test/strong")
    monkeypatch.setattr(settings, "EXTRACTION_REASONING_EFFORT_FAST", "low")
    monkeypatch.setattr(settings, "EXTRACTION_REASONING_EFFORT_STRONG", "high")


def _payload(**overrides) -> dict:
    salary_at = SOURCE.index("up to $3500")
    period_at = SOURCE.index("per month")
    job = {
        "job_title": {
            "value": "Finance officer",
            "evidence": "Finance officer",
            "start_char": 0,
            "end_char": 15,
            "confidence": 0.95,
        },
        "salary": {
            "value": "3500",
            "evidence": "up to $3500",
            "start_char": salary_at,
            "end_char": salary_at + len("up to $3500"),
            "confidence": 0.9,
        },
        "salary_period": {
            "value": "month",
            "evidence": "per month",
            "start_char": period_at,
            "end_char": period_at + len("per month"),
            "confidence": 0.9,
        },
    }
    job.update(overrides)
    return {"jobs": [job]}


def _response(**overrides) -> ExtractionResponse:
    return ExtractionResponse.model_validate(_payload(**overrides))


# A span that is nowhere in the source. The model sounds certain; the offsets
# are the thing that gives it away.
_FABRICATED = {
    "value": "9999",
    "evidence": "salary is SGD 9,999",
    "start_char": 0,
    "end_char": 19,
    "confidence": 0.99,
}


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
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " received_datetime, processing_status, source_state, classification_status,"
            " body_html_r2_key, subject, sender_email)"
            " VALUES (:i, :t, :m, 'MSG-1', now(), 'classified', 'present',"
            " 'recruitment', :key, 'Finance officer', 'evelyn@example.com')"
        ),
        {
            "i": row_id,
            "t": tenant_id,
            "m": mailbox_id,
            "key": body_key(tenant_id, mailbox_id, "MSG-1", "html"),
        },
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id, row_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id}
    )
    await admin_session.commit()


# --- persistence ------------------------------------------------------------


async def test_persist_writes_an_opportunity_with_a_parsed_salary(
    admin_session, email_row
):
    _, _, eid = email_row
    tid = email_row[0]

    await persist(tid, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE)

    row = (
        await admin_session.execute(
            text(
                "SELECT job_title_raw, salary_min, salary_max, salary_period,"
                " quality_state, received_datetime FROM opportunities"
                " WHERE email_message_id = :e"
            ),
            {"e": eid},
        )
    ).one()
    assert row.job_title_raw == "Finance officer"
    assert float(row.salary_min) == 3500.0
    assert row.salary_period == "month"
    assert row.quality_state == "verified"
    assert row.received_datetime is not None, "denormalised for sorting"


async def test_persist_records_evidence_with_its_validity(admin_session, email_row):
    tid, _, eid = email_row

    await persist(tid, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE)

    rows = (
        await admin_session.execute(
            text(
                "SELECT field_name, evidence_valid FROM extraction_evidence ev"
                " JOIN extractions ex ON ex.id = ev.extraction_id"
                " WHERE ex.email_message_id = :e"
            ),
            {"e": eid},
        )
    ).all()
    assert {r.field_name for r in rows} >= {"job_title", "salary", "salary_period"}
    assert all(r.evidence_valid for r in rows)


async def test_a_fabricated_span_is_recorded_as_invalid_rather_than_dropped(
    admin_session, email_row
):
    """The record that the model claimed something untrue is the point."""
    tid, _, eid = email_row

    await persist(
        tid,
        eid,
        _response(salary=_FABRICATED),
        LLMResult(data={}, model="test/fast"),
        SOURCE,
    )

    valid = (
        await admin_session.execute(
            text(
                "SELECT evidence_valid FROM extraction_evidence ev"
                " JOIN extractions ex ON ex.id = ev.extraction_id"
                " WHERE ex.email_message_id = :e AND ev.field_name = 'salary'"
            ),
            {"e": eid},
        )
    ).scalar_one()
    assert valid is False

    review = (
        await admin_session.execute(
            text(
                "SELECT review_status FROM opportunities WHERE email_message_id = :e"
            ),
            {"e": eid},
        )
    ).scalar_one()
    assert review == "needs_review", "a failed check must reach a human"


async def test_a_run_that_finds_nothing_still_records_an_extraction(
    admin_session, email_row
):
    tid, _, eid = email_row

    ids = await persist(
        tid,
        eid,
        ExtractionResponse(jobs=[]),
        LLMResult(data={}, model="test/fast"),
        SOURCE,
    )

    assert ids == []
    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM extractions WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 1, "the zero-vacancy run is the one worth inspecting"


async def test_replay_never_overwrites_a_human_correction(admin_session, email_row):
    """Criterion 5. A prompt upgrade must not undo a recruiter's fix."""
    tid, _, eid = email_row
    ids = await persist(
        tid, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )

    await admin_session.execute(
        text(
            "INSERT INTO opportunity_field_overrides (id, tenant_id, opportunity_id,"
            " field_name, ai_value, human_value) VALUES (:i, :t, :o, 'location_raw',"
            " 'Maybank Tower', 'Raffles Place')"
        ),
        {"i": uuid.uuid4(), "t": tid, "o": ids[0]},
    )
    await admin_session.commit()

    await persist(tid, eid, _response(), LLMResult(data={}, model="test/strong"), SOURCE)

    override = (
        await admin_session.execute(
            text(
                "SELECT human_value FROM opportunity_field_overrides"
                " WHERE opportunity_id = :o"
            ),
            {"o": ids[0]},
        )
    ).scalar_one()
    assert override == "Raffles Place"


async def test_three_jobs_in_one_email_become_three_rows(admin_session, email_row):
    tid, _, eid = email_row
    one = _payload()["jobs"][0]
    response = ExtractionResponse.model_validate({"jobs": [one, one, one]})

    ids = await persist(
        tid, eid, response, LLMResult(data={}, model="test/fast"), SOURCE
    )

    assert len(ids) == 3


async def test_the_prompt_version_is_stamped_on_the_run(admin_session, email_row):
    """Without it a quality regression cannot be told from a change in the mail."""
    tid, _, eid = email_row

    await persist(tid, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE)

    version = (
        await admin_session.execute(
            text("SELECT prompt_version FROM extractions WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert version == settings.PROMPT_VERSION


# --- model routing (§32) ----------------------------------------------------


class _Spy:
    """Records the models it was asked for, and answers from a queue."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.models: list[str] = []
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, model, schema, **kw):
        self.models.append(model)
        self.calls.append({"model": model, "schema": schema, **kw})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(data=answer, model=model)


async def test_a_clean_fast_answer_never_reaches_the_strong_model():
    """The saving is the whole point of two models."""
    llm = _Spy(_payload())

    await extract(SOURCE, llm=llm)

    assert llm.models == [settings.EXTRACTION_MODEL_FAST]


async def test_an_unusable_fast_answer_escalates():
    llm = _Spy(LLMInvalidJSON("not json"), _payload())

    response, result = await extract(SOURCE, llm=llm)

    assert llm.models == [
        settings.EXTRACTION_MODEL_FAST,
        settings.EXTRACTION_MODEL_STRONG,
    ]
    assert result.model == settings.EXTRACTION_MODEL_STRONG
    assert len(response.jobs) == 1


async def test_a_fabricated_span_escalates_however_confident_the_model_sounded():
    """Escalation is decided by a check we can make, not by self-reported 0.99."""
    llm = _Spy(_payload(salary=_FABRICATED), _payload())

    _, result = await extract(SOURCE, llm=llm)

    assert llm.models == [
        settings.EXTRACTION_MODEL_FAST,
        settings.EXTRACTION_MODEL_STRONG,
    ]
    assert result.model == settings.EXTRACTION_MODEL_STRONG


async def test_an_email_with_no_vacancy_does_not_escalate():
    """Most mail genuinely contains none; paying twice to be told so is waste."""
    llm = _Spy({"jobs": []})

    await extract(SOURCE, llm=llm)

    assert llm.models == [settings.EXTRACTION_MODEL_FAST]


async def test_both_models_failing_raises_rather_than_inventing_an_empty_result():
    llm = _Spy(LLMInvalidJSON("a"), LLMInvalidJSON("b"))

    with pytest.raises(LLMInvalidJSON):
        await extract(SOURCE, llm=llm)


async def test_the_imperfect_answer_is_kept_when_both_models_fall_short():
    """`needs_review` is a vacancy a recruiter can still correct; nothing is
    gained by throwing it away because it was imperfect."""
    llm = _Spy(_payload(salary=_FABRICATED), _payload(salary=_FABRICATED))

    response, result = await extract(SOURCE, llm=llm)

    assert len(response.jobs) == 1
    assert result.model == settings.EXTRACTION_MODEL_STRONG


async def test_extraction_never_asks_a_provider_to_compile_the_schema():
    """The production outage, as an assertion.

    A strict `json_schema` response format built from twelve nested fields was
    rejected outright — "the compiled grammar is too large" — on every email,
    forever. `schema=None` asks for a plain JSON object; the schema travels as
    prompt text, which nothing compiles and nothing can refuse.
    """
    llm = _Spy(_payload())

    await extract(SOURCE, llm=llm)

    assert llm.calls[0]["schema"] is None


async def test_extraction_goes_to_cerebras_with_a_token_budget():
    llm = _Spy(_payload())

    await extract(SOURCE, llm=llm)

    call = llm.calls[0]
    assert call["base_url"] == settings.CEREBRAS_BASE_URL
    assert call["api_key"] == settings.CEREBRAS_API_KEY
    assert call["extra_body"]["max_tokens"] == settings.EXTRACTION_MAX_TOKENS


async def test_escalation_raises_the_reasoning_effort():
    """§32 without a request shape that can 400.

    Escalation used to mean a different, larger model, and that model is what
    rejected the request. The same endpoint thinking harder cannot fail in a
    way the first call did not.
    """
    llm = _Spy(_payload(salary=_FABRICATED), _payload())

    await extract(SOURCE, llm=llm)

    assert [c["extra_body"]["reasoning_effort"] for c in llm.calls] == [
        settings.EXTRACTION_REASONING_EFFORT_FAST,
        settings.EXTRACTION_REASONING_EFFORT_STRONG,
    ]


async def test_an_unset_strong_model_escalates_by_effort_on_the_fast_one(monkeypatch):
    """Never an empty model id: that is a request the provider rejects."""
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_STRONG", "")
    llm = _Spy(_payload(salary=_FABRICATED), _payload())

    await extract(SOURCE, llm=llm)

    assert llm.models == ["test/fast", "test/fast"]
    assert llm.calls[1]["extra_body"]["reasoning_effort"] == "high"


async def test_the_models_come_from_settings(monkeypatch):
    """A model id in source is a defect (Global Constraints)."""
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "swapped/fast")
    llm = _Spy(_payload())

    await extract(SOURCE, llm=llm)

    assert llm.models == ["swapped/fast"]


async def test_the_email_text_reaches_the_prompt():
    llm = _Spy(_payload())
    captured: list[str] = []

    async def spy(prompt, *, model, schema, **kw):
        captured.append(prompt)
        return await llm(prompt, model=model, schema=schema, **kw)

    await extract(SOURCE, llm=spy)

    assert SOURCE in captured[0]


# --- the job ----------------------------------------------------------------


def _wire(monkeypatch, tenant_id, mailbox_id, response_payload):
    """Fake R2, fake model. Nothing reaches the network."""
    store = InMemoryBodyStore()
    key = body_key(tenant_id, mailbox_id, "MSG-1", "html")
    store.objects[key] = f"<p>{SOURCE}</p>"
    monkeypatch.setattr(jobs, "body_store", lambda: store)

    llm = _Spy(response_payload)
    _fake_extract(monkeypatch, llm)
    return store, llm


def _fake_extract(monkeypatch, llm):
    """Bind the real `extract` *before* patching the name.

    Looking it up inside the replacement would find the replacement — the job
    imports the module attribute at call time, which is what makes the patch
    reachable at all.
    """
    real = extract_module.extract

    async def _extract(source, **_):
        return await real(source, llm=llm)

    monkeypatch.setattr(extract_module, "extract", _extract)


async def _status(admin_session, eid):
    return (
        await admin_session.execute(
            text("SELECT processing_status FROM email_messages WHERE id = :i"),
            {"i": eid},
        )
    ).scalar_one()


async def test_the_job_moves_a_classified_row_to_extracted(
    admin_session, email_row, monkeypatch
):
    tid, mid, eid = email_row
    # Offsets are into `to_text` output, not the HTML, so the job's own
    # preprocessing has to produce a source the spans land in. Rather than
    # guess at it, the response is built from whatever `to_text` returns.
    from app.services.ingest.preprocess import to_text

    source = to_text(f"<p>{SOURCE}</p>", subject="Finance officer", sender="e@x.com")
    at = source.index("Finance officer")
    payload = {
        "jobs": [
            {
                "job_title": {
                    "value": "Finance officer",
                    "evidence": "Finance officer",
                    "start_char": at,
                    "end_char": at + len("Finance officer"),
                    "confidence": 0.95,
                }
            }
        ]
    }
    _wire(monkeypatch, tid, mid, payload)

    await jobs.extract_email(
        {}, email_message_id=str(eid), tenant_id=str(tid), mailbox_id=str(mid)
    )

    assert await _status(admin_session, eid) == "extracted"
    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM opportunities WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 1


async def test_an_email_with_no_vacancy_ends_at_no_opportunity(
    admin_session, email_row, monkeypatch
):
    """Not a failure: the gate fails open, so plenty of this arrives correctly
    empty, and `failed` would send a human to look at nothing."""
    tid, mid, eid = email_row
    _wire(monkeypatch, tid, mid, {"jobs": []})

    await jobs.extract_email(
        {}, email_message_id=str(eid), tenant_id=str(tid), mailbox_id=str(mid)
    )

    assert await _status(admin_session, eid) == "no_opportunity"


async def test_an_already_extracted_row_is_not_extracted_again(
    admin_session, email_row, monkeypatch
):
    """Model calls cost money; `rescan_stuck` and the gate both name this job."""
    tid, mid, eid = email_row
    await admin_session.execute(
        text("UPDATE email_messages SET processing_status = 'extracted' WHERE id = :i"),
        {"i": eid},
    )
    await admin_session.commit()
    _, llm = _wire(monkeypatch, tid, mid, _payload())

    await jobs.extract_email(
        {}, email_message_id=str(eid), tenant_id=str(tid), mailbox_id=str(mid)
    )

    assert llm.models == [], "no model call for a row that is already done"


async def test_a_row_interrupted_mid_call_is_resumed(
    admin_session, email_row, monkeypatch
):
    """`extracting` is what a killed worker leaves behind, and what
    `rescan_stuck` re-enqueues. Refusing it would strand the row forever."""
    tid, mid, eid = email_row
    await admin_session.execute(
        text("UPDATE email_messages SET processing_status = 'extracting' WHERE id = :i"),
        {"i": eid},
    )
    await admin_session.commit()
    _, llm = _wire(monkeypatch, tid, mid, {"jobs": []})

    await jobs.extract_email(
        {}, email_message_id=str(eid), tenant_id=str(tid), mailbox_id=str(mid)
    )

    assert llm.models != []
    assert await _status(admin_session, eid) == "no_opportunity"


async def test_an_unusable_answer_from_both_models_marks_the_row_failed(
    admin_session, email_row, monkeypatch
):
    tid, mid, eid = email_row
    store = InMemoryBodyStore()
    store.objects[body_key(tid, mid, "MSG-1", "html")] = "<p>x</p>"
    monkeypatch.setattr(jobs, "body_store", lambda: store)
    llm = _Spy(LLMInvalidJSON("a"), LLMInvalidJSON("b"))
    _fake_extract(monkeypatch, llm)

    await jobs.extract_email(
        {}, email_message_id=str(eid), tenant_id=str(tid), mailbox_id=str(mid)
    )

    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, last_error FROM email_messages WHERE id = :i"
            ),
            {"i": eid},
        )
    ).one()
    assert row.processing_status == "failed"
    assert row.last_error


async def test_an_unconfigured_deployment_refuses_rather_than_guessing(
    email_row, monkeypatch
):
    """Mirrors `classify_email`: a missing setting must be an error, not a
    quietly plausible pipeline."""
    tid, mid, eid = email_row
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "")

    with pytest.raises(RuntimeError):
        await jobs.extract_email(
            {}, email_message_id=str(eid), tenant_id=str(tid), mailbox_id=str(mid)
        )


async def test_a_job_naming_the_wrong_mailbox_does_nothing(
    admin_session, email_row, monkeypatch
):
    """The routing ids in the payload are self-validating: a mismatched pair
    reads no row and quietly no-ops rather than extracting the wrong email.

    Asserted on the mailbox rather than the tenant because the tenant half is
    enforced by RLS, which the local superuser bypasses — that would test the
    developer's database role, not the job.
    """
    tid, mid, eid = email_row
    _, llm = _wire(monkeypatch, tid, mid, _payload())

    await jobs.extract_email(
        {},
        email_message_id=str(eid),
        tenant_id=str(tid),
        mailbox_id=str(uuid.uuid4()),
    )

    assert llm.models == []
    assert await _status(admin_session, eid) == "classified"
