"""The mailbox was the transport; the client assignment is the authority.

Recruiter A may receive the client's email, but if the client belongs to
recruiter B, the job order is B's. A keeps sight of it through the
mailbox-owner term of the visibility predicate, which is not this module's
business.
"""

import uuid

import pytest
from sqlalchemy import text

from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user

_SOURCE = "Vacancy at Acme Pte Ltd. Finance officer role, up to $3500 per month."


def _extraction(company_name: str | None) -> tuple[ExtractionResponse, LLMResult, str]:
    """One vacancy, built the way `tests/test_client_ingestion.py` builds one."""
    salary_at = _SOURCE.index("up to $3500")
    period_at = _SOURCE.index("per month")
    job: dict = {
        "job_title": {
            "value": "Finance officer",
            "evidence": "Finance officer",
            "start_char": _SOURCE.index("Finance officer"),
            "end_char": _SOURCE.index("Finance officer") + len("Finance officer"),
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
    if company_name is not None:
        job["company"] = {
            "value": company_name,
            "evidence": "Acme Pte Ltd",
            "start_char": _SOURCE.index("Acme Pte Ltd"),
            "end_char": _SOURCE.index("Acme Pte Ltd") + len("Acme Pte Ltd"),
            "confidence": 0.9,
        }
    response = ExtractionResponse.model_validate({"jobs": [job]})
    return response, LLMResult(data={}, model="test/fast"), _SOURCE


@pytest.fixture
def captured_events(monkeypatch) -> list:
    """What ingestion asked to be sent, without a notification catalogue.

    Patched on `app.services.ingest.persist`, because that module imported the
    name — rebinding at the source would not be seen.
    """
    events: list = []

    async def _capture(event, session) -> list:
        events.append(event)
        return []

    monkeypatch.setattr("app.services.ingest.persist.emit", _capture)
    return events


async def _second_user(tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role)"
                " VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": user_id, "t": tenant_id, "e": email},
        )
        await s.commit()
    return user_id


async def _mailbox_message(
    tenant_id: uuid.UUID,
    *,
    owner_user_id: uuid.UUID | None,
    sender_email: str,
) -> uuid.UUID:
    """A message that landed in `owner_user_id`'s mailbox."""
    mailbox_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, status, retention_months)"
                " VALUES (:i, :t, :u, :ms, 'inbox', 'folder', 'active', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id, "u": owner_user_id, "ms": mailbox_id.hex[:12]},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email)"
                " VALUES (:i, :t, :m, :g, 'Vacancy', :s)"
            ),
            {
                "i": message_id,
                "t": tenant_id,
                "m": mailbox_id,
                "g": f"MSG-{message_id.hex[:8]}",
                "s": sender_email,
            },
        )
        await s.commit()
    return message_id


async def _client_owned_by(
    tenant_id: uuid.UUID, user_id: uuid.UUID, *, domain: str, name: str
) -> uuid.UUID:
    client_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized,"
                " email_domain, status, assigned_user_id)"
                " VALUES (:i, :t, :n, :nn, :d, 'confirmed', :u)"
            ),
            {
                "i": client_id,
                "t": tenant_id,
                "n": name,
                "nn": name.lower(),
                "d": domain,
                "u": user_id,
            },
        )
        await s.commit()
    return client_id


async def _opportunity_row(opportunity_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT client_id, assigned_user_id FROM opportunities"
                    " WHERE id = :i"
                ),
                {"i": opportunity_id},
            )
        ).one()


async def test_ingested_job_order_goes_to_the_clients_recruiter_not_the_mailbox_owner(
    captured_events,
) -> None:
    """The mailbox was the transport. The client assignment is the authority."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter_a = await seed_tenant_with_user()
    try:
        recruiter_b = await _second_user(tenant_id, "b@example.test")
        client_id = await _client_owned_by(
            tenant_id, recruiter_b, domain="acme.com.sg", name="Acme Pte Ltd"
        )
        # The mail lands in A's mailbox; the client is B's.
        message_id = await _mailbox_message(
            tenant_id, owner_user_id=recruiter_a, sender_email="hr@acme.com.sg"
        )

        response, result, source = _extraction("Acme Pte Ltd")
        ids = await persist(tenant_id, message_id, response, result, source=source)

        assert len(ids) == 1
        row = await _opportunity_row(ids[0])
        assert row.client_id == client_id
        assert row.assigned_user_id == recruiter_b

        # Only B hears about it. `None` here would tell the whole agency.
        assert len(captured_events) == 1, captured_events
        assert captured_events[0].recipient_user_ids == (recruiter_b,)
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_unmatched_client_leaves_the_job_order_on_the_queue(
    captured_events,
) -> None:
    """No domain and no company name: both columns stay NULL, everyone is told."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter_a = await seed_tenant_with_user()
    try:
        message_id = await _mailbox_message(
            tenant_id, owner_user_id=recruiter_a, sender_email=None
        )
        response, result, source = _extraction(None)
        ids = await persist(tenant_id, message_id, response, result, source=source)

        row = await _opportunity_row(ids[0])
        assert row.client_id is None
        assert row.assigned_user_id is None

        # Queue work concerns everybody: None, not an empty tuple.
        assert len(captured_events) == 1, captured_events
        assert captured_events[0].recipient_user_ids is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_matched_client_with_no_recruiter_still_leaves_it_on_the_queue(
    captured_events,
) -> None:
    """A client nobody owns yet: the client is recorded, the assignee is not."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter_a = await seed_tenant_with_user()
    try:
        message_id = await _mailbox_message(
            tenant_id, owner_user_id=recruiter_a, sender_email="hr@acme.com.sg"
        )
        response, result, source = _extraction("Acme Pte Ltd")
        ids = await persist(tenant_id, message_id, response, result, source=source)

        row = await _opportunity_row(ids[0])
        assert row.client_id is not None
        assert row.assigned_user_id is None
        assert captured_events[0].recipient_user_ids is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_rerunning_extraction_does_not_take_back_a_claimed_job_order(
    captured_events,
) -> None:
    """`extract_email` re-runs after a crash. A claim must survive the replay."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter_a = await seed_tenant_with_user()
    try:
        recruiter_b = await _second_user(tenant_id, "b@example.test")
        claimer = await _second_user(tenant_id, "c@example.test")
        await _client_owned_by(
            tenant_id, recruiter_b, domain="acme.com.sg", name="Acme Pte Ltd"
        )
        message_id = await _mailbox_message(
            tenant_id, owner_user_id=recruiter_a, sender_email="hr@acme.com.sg"
        )

        response, result, source = _extraction("Acme Pte Ltd")
        ids = await persist(tenant_id, message_id, response, result, source=source)
        opportunity_id = ids[0]
        assert (await _opportunity_row(opportunity_id)).assigned_user_id == recruiter_b

        # A recruiter takes it over by hand.
        async with AdminSessionLocal() as s:
            await s.execute(
                text(
                    "UPDATE opportunities SET assigned_user_id = :u WHERE id = :i"
                ),
                {"u": claimer, "i": opportunity_id},
            )
            await s.commit()

        # The worker crashed and the email is extracted again.
        await persist(tenant_id, message_id, response, result, source=source)

        row = await _opportunity_row(opportunity_id)
        assert row.assigned_user_id == claimer, (
            "a replay recomputed the assignee and stole a claimed job order"
        )
    finally:
        await cleanup_tenant(tenant_id)
