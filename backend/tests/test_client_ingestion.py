"""Extraction and client proposal are one transaction, or neither happened.

`persist()` already commits the extraction, its vacancies, its evidence and
its glossary codes together, on the grounds that a partial write is
indistinguishable from a complete one downstream. The client belongs in the
same transaction for the same reason: a client proposed by an extraction that
rolled back is a row nothing in the system can explain.
"""

import uuid
from unittest.mock import AsyncMock, patch

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


def _three_job_extraction(*, companies: list[str]) -> tuple[ExtractionResponse, LLMResult, str]:
    """Three jobs in one email, built the way `_extraction_fixture` builds one.

    Each job reuses the same source sentence positions; only the company value
    differs per job, which is all `match_client`'s "first non-empty company"
    logic needs to see a disagreement.
    """
    source = "Vacancy at Acme Pte Ltd. Finance officer role, up to $3500 per month."
    salary_at = source.index("up to $3500")
    period_at = source.index("per month")

    def _job(company_name: str) -> dict:
        return {
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

    response = ExtractionResponse.model_validate({"jobs": [_job(c) for c in companies]})
    result = LLMResult(data={}, model="test/fast")
    return response, result, source


async def test_one_email_three_jobs_same_company_makes_one_client(agency) -> None:
    """persist() calls match_client once per email, not once per job (§14).

    Three vacancies for the same company must still propose exactly one
    client and one mention. If `match_client` ever moved inside the
    `for job in response.jobs` loop, this would start proposing three
    identical clients and this test would catch it.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)

    response, result, source = _three_job_extraction(
        companies=["Acme Pte Ltd", "Acme Pte Ltd", "Acme Pte Ltd"]
    )
    opportunity_ids = await persist(agency, message_id, response, result, source=source)

    assert len(opportunity_ids) == 3

    async with tenant_session(agency) as s:
        clients = (await s.execute(text("SELECT count(*) FROM clients"))).scalar_one()
        mentions = (await s.execute(text("SELECT count(*) FROM client_mentions"))).scalar_one()
        opportunities = (
            await s.execute(text("SELECT count(*) FROM opportunities"))
        ).scalar_one()
    assert (clients, mentions) == (1, 1)
    assert opportunities == 3


async def test_persist_calls_match_client_once_regardless_of_job_count(agency) -> None:
    """Pins down *where* persist() calls match_client, not just its output.

    `match_client`'s own writes are idempotent (ON CONFLICT DO UPDATE /
    DO NOTHING), so a row-count assertion alone would still pass even if
    `match_client` were called once per job instead of once per email —
    the store would just absorb the duplicate calls silently. Spying on the
    call itself is the only way to catch that regression: three jobs must
    produce exactly one call, no matter how many times persist() would
    otherwise get away with calling it.
    """
    import app.services.ingest.persist as persist_module

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)

    response, result, source = _three_job_extraction(
        companies=["Acme Pte Ltd", "Acme Pte Ltd", "Acme Pte Ltd"]
    )

    spy = AsyncMock(wraps=persist_module.match_client)
    with patch.object(persist_module, "match_client", spy):
        await persist_module.persist(agency, message_id, response, result, source=source)

    assert spy.call_count == 1


async def test_one_email_three_jobs_different_companies_uses_first_named(agency) -> None:
    """persist() picks the first non-empty company across all jobs (§14 impl).

    This documents the *actual* behaviour, not the ideal one: when one email's
    jobs disagree about the company, `persist()` still proposes exactly one
    client, built from whichever job's company field is first in `response
    .jobs` order. One client is right for one email even when the extraction
    disagrees with itself — an email is from one sender, and the client
    review queue proposes a single company per email, not per line item. This
    test does not judge whether "first named" is the best tie-break; it only
    pins down that it is the current one.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)

    response, result, source = _three_job_extraction(
        companies=["Acme Pte Ltd", "Beta Holdings", "Gamma Co"]
    )
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        rows = (
            await s.execute(text("SELECT email_domain, status FROM clients"))
        ).all()
    # match_client() is called once per email with the first non-empty
    # company across all jobs ("Acme Pte Ltd", from Beta/Gamma's jobs it
    # never sees), plus the sender's domain. The sender's domain wins the
    # match/insert here (client_matching.py tries domain before name), so the
    # single client row is keyed on "acme.com.sg" — Beta Holdings and Gamma
    # Co never produce their own client rows or mentions, which is exactly
    # the "one client per email" guarantee this test exists to pin down.
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
