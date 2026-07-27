"""Extraction and client proposal are one transaction, or neither happened.

`persist()` already commits the extraction, its vacancies, its evidence and
its glossary codes together, on the grounds that a partial write is
indistinguishable from a complete one downstream. The client belongs in the
same transaction for the same reason: a client proposed by an extraction that
rolled back is a row nothing in the system can explain.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult
from tests.conftest import AdminSessionLocal


def _extraction_fixture(*, company_name: str) -> tuple[ExtractionResponse, LLMResult]:
    """Copied from tests/test_extract_job.py's `_payload`/`_response` helpers.

    Rebuilt here rather than imported so this test breaks the moment the real
    extraction contract changes shape, instead of silently tracking it.
    """
    source = "Vacancy at Acme Pte Ltd. Finance officer role, up to $3500 per month."
    salary_at = source.index("up to $3500")
    period_at = source.index("per month")
    job = {
        "company": {
            "value": company_name,
            "evidence": "Acme Pte Ltd",
            "start_char": source.index("Acme Pte Ltd"),
            "end_char": source.index("Acme Pte Ltd") + len("Acme Pte Ltd"),
            "confidence": 0.9,
        },
        "job_title": {
            "value": "Finance officer",
            "evidence": "Finance officer",
            "start_char": source.index("Finance officer"),
            "end_char": source.index("Finance officer") + len("Finance officer"),
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
    response = ExtractionResponse.model_validate({"jobs": [job]})
    result = LLMResult(data={}, model="test/fast")
    return response, result, source


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        for table in ("client_mentions", "clients", "email_messages", "mailboxes"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _insert_message(tenant_id: uuid.UUID, message_id: uuid.UUID) -> None:
    mailbox_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope,"
                " status, retention_months)"
                " VALUES (:i, :t, 'ms-user-1', 'inbox', 'folder', 'active', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email)"
                " VALUES (:i, :t, :m, 'MSG-1', 'Vacancy', 'hr@acme.com.sg')"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id},
        )
        await s.commit()


async def test_persisting_an_extraction_proposes_the_sender_as_a_client(agency) -> None:
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)

    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        rows = (
            await s.execute(text("SELECT email_domain, status FROM clients"))
        ).all()
    assert rows == [("acme.com.sg", "unconfirmed")]


async def test_running_persist_twice_leaves_one_client_and_one_mention(agency) -> None:
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)

    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source=source)
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        clients = (await s.execute(text("SELECT count(*) FROM clients"))).scalar_one()
        mentions = (await s.execute(text("SELECT count(*) FROM client_mentions"))).scalar_one()
    assert (clients, mentions) == (1, 1)
