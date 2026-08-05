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
from tests.conftest import AdminSessionLocal, cleanup_tenant


def _extraction_fixture(*, company_name: str | None) -> tuple[ExtractionResponse, LLMResult, str]:
    """Copied from tests/test_extract_job.py's `_payload`/`_response` helpers.

    Rebuilt here rather than imported so this test breaks the moment the real
    extraction contract changes shape, instead of silently tracking it.
    """
    source = "Vacancy at Acme Pte Ltd. Finance officer role, up to $3500 per month."
    salary_at = source.index("up to $3500")
    period_at = source.index("per month")
    job: dict = {}
    if company_name is not None:
        job["company"] = {
            "value": company_name,
            "evidence": "Acme Pte Ltd",
            "start_char": source.index("Acme Pte Ltd"),
            "end_char": source.index("Acme Pte Ltd") + len("Acme Pte Ltd"),
            "confidence": 0.9,
        }
    job["job_title"] = {
        "value": "Finance officer",
        "evidence": "Finance officer",
        "start_char": source.index("Finance officer"),
        "end_char": source.index("Finance officer") + len("Finance officer"),
        "confidence": 0.95,
    }
    job["salary"] = {
        "value": "3500",
        "evidence": "up to $3500",
        "start_char": salary_at,
        "end_char": salary_at + len("up to $3500"),
        "confidence": 0.9,
    }
    job["salary_period"] = {
        "value": "month",
        "evidence": "per month",
        "start_char": period_at,
        "end_char": period_at + len("per month"),
        "confidence": 0.9,
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
    await cleanup_tenant(tid)


async def _insert_message(
    tenant_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    sender_email: str = "hr@acme.com.sg",
    sender_name: str | None = None,
    mailbox_owner_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Seed a mailbox (optionally owned by a recruiter) and one email on it.

    Returns the mailbox id, so a test that needs to point a *second* email at
    the same mailbox can. The mailbox owner is what drives the assignment
    rule, and the sender columns drive contact capture — both are optional so
    the existing tests that need neither keep their defaults.
    """
    mailbox_id = uuid.uuid4()
    ms_user_id = f"ms-user-{mailbox_id.hex[:8]}"
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, status, retention_months)"
                " VALUES (:i, :t, :u, :ms, 'inbox', 'folder', 'active', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id, "u": mailbox_owner_id, "ms": ms_user_id},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, subject,"
                "  sender_email, sender_name)"
                " VALUES (:i, :t, :m, 'MSG-1', 'Vacancy', :se, :sn)"
            ),
            {
                "i": message_id,
                "t": tenant_id,
                "m": mailbox_id,
                "se": sender_email,
                "sn": sender_name,
            },
        )
        await s.commit()
    return mailbox_id


async def test_persisting_an_extraction_proposes_the_body_company_as_a_client(agency) -> None:
    """The company named in the body is the client, not the sender's domain.

    A forwarded job order names the hiring company; the sender's domain (here
    the forwarding agency's) is not attached to the client.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)

    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        rows = (await s.execute(text("SELECT email_domain, status FROM clients"))).all()
    # domain is NULL: the body company created the client, and the sender's
    # domain is not attached (the sender may be an intermediary).
    assert rows == [(None, "unconfirmed")]


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
        opportunities = (await s.execute(text("SELECT count(*) FROM opportunities"))).scalar_one()
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
        rows = (await s.execute(text("SELECT name_normalized, email_domain FROM clients"))).all()
    # The first non-empty company across all jobs ("Acme Pte Ltd") creates
    # one client. The sender's domain is not attached — the body company is
    # the authority. Beta Holdings and Gamma Co never produce their own rows,
    # which is the "one client per email" guarantee this test pins down.
    assert rows == [("acme", None)]


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


async def _a_recruiter(tenant_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'recruiter')"),
            {"i": user_id, "t": tenant_id, "e": f"{user_id.hex[:8]}@example.test"},
        )
        await s.commit()
    return user_id


async def test_a_new_client_is_assigned_to_the_mailbox_owner(agency) -> None:
    """The recruiter whose mailbox received the email becomes the client's owner.

    A client is created on the first email from a domain, and the mailbox
    owner — the person the client emailed to — is written as
    `assigned_user_id`. That is the "first person the client emailed to"
    rule: the owner is decided once, at creation, not on every re-match.
    """
    from app.services.ingest.persist import persist

    owner = await _a_recruiter(agency)
    message_id = uuid.uuid4()
    await _insert_message(agency, message_id, mailbox_owner_id=owner)

    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        assigned = (await s.execute(text("SELECT assigned_user_id FROM clients"))).scalar_one()
    assert assigned == owner


async def test_a_second_email_preserves_the_first_owner(agency) -> None:
    """A forwarded email never reassigns the client.

    Client emails recruiter A; A forwards to B. The client was created on A's
    email, so A is the owner. The same client emailing B's mailbox re-matches
    the existing row, and `_INSERT_CLIENT`'s ON CONFLICT DO UPDATE SET
    last_seen_at touches nothing else — B never becomes the owner.
    """
    from app.services.ingest.persist import persist

    owner_a = await _a_recruiter(agency)
    owner_b = await _a_recruiter(agency)

    msg_a = uuid.uuid4()
    await _insert_message(agency, msg_a, mailbox_owner_id=owner_a)
    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, msg_a, response, result, source=source)

    # Same client domain, different mailbox owned by B.
    msg_b = uuid.uuid4()
    await _insert_message(agency, msg_b, mailbox_owner_id=owner_b)
    await persist(agency, msg_b, response, result, source=source)

    async with tenant_session(agency) as s:
        row = (
            await s.execute(
                text("SELECT assigned_user_id, count(*) FROM clients GROUP BY assigned_user_id")
            )
        ).one()
    assert row.assigned_user_id == owner_a
    assert row.count == 1


async def test_a_domain_matched_sender_becomes_a_contact(agency) -> None:
    """A sender on the client's own domain is a person at that company.

    Contact capture only fires on a domain match — the body names no company,
    so the sender's domain is the fallback that creates the client. The display
    name from the email header is the contact's name; when it is absent the
    email address stands in (the column is NOT NULL).
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(
        agency,
        message_id,
        sender_email="jane.doe@acme.com.sg",
        sender_name="Jane Doe",
    )
    # No company in the body — the match falls to the sender's domain.
    response, result, source = _extraction_fixture(company_name=None)
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        rows = (await s.execute(text("SELECT name, email, is_primary FROM client_contacts"))).all()
    assert rows == [("Jane Doe", "jane.doe@acme.com.sg", False)]


async def test_a_body_company_match_creates_no_contact(agency) -> None:
    """A body-company match creates no contact — the sender may be an intermediary.

    When the company is named in the body, the client is matched by name and
    the sender is NOT captured as a contact. A forwarded job order's sender is
    a recruiter at an agency, not a person at the hiring company, so capturing
    them would be a fabrication.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(
        agency,
        message_id,
        sender_email="jocelynchan@recruitexpress.com.sg",
        sender_name="Jocelyn Chan",
    )
    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        contacts = (await s.execute(text("SELECT count(*) FROM client_contacts"))).scalar_one()
    assert contacts == 0


async def test_reprocessing_creates_no_duplicate_contact(agency) -> None:
    """A retried extraction does not add the same contact twice.

    The `_INSERT_CONTACT` guard is `WHERE NOT EXISTS` on the lowercased email,
    so a `rescan_stuck` re-run that reaches the same sender is a no-op rather
    than a second row.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(
        agency,
        message_id,
        sender_email="jane.doe@acme.com.sg",
        sender_name="Jane Doe",
    )
    response, result, source = _extraction_fixture(company_name=None)
    await persist(agency, message_id, response, result, source=source)
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        contacts = (await s.execute(text("SELECT count(*) FROM client_contacts"))).scalar_one()
    assert contacts == 1


async def test_a_forwarded_email_captures_the_original_sender_as_buddy(agency) -> None:
    """A forwarded email's original sender is a buddy, not a client contact.

    The original sender (Topaz) is an external recruiter who referred the
    client — she is NOT a Wearnes employee. So she becomes a buddy with a
    referral, not a client contact. The forwarder (Jocelyn) is nobody.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    await _insert_message(
        agency,
        message_id,
        sender_email="jocelynchan@recruitexpress.com.sg",
        sender_name="Jocelyn Chan",
    )
    response, result, source = _extraction_fixture(company_name="Wearnes Automotive")
    await persist(
        agency,
        message_id,
        response,
        result,
        source=source,
        original_sender_email="topaz@recruitexpress.com.sg",
        original_sender_name="Topaz Liang",
    )

    async with tenant_session(agency) as s:
        contacts = (await s.execute(text("SELECT count(*) FROM client_contacts"))).scalar_one()
        buddies = (await s.execute(text("SELECT name, email, email_domain FROM buddies"))).all()
        referrals = (await s.execute(text("SELECT count(*) FROM buddy_referrals"))).scalar_one()

    assert contacts == 0, "a forwarded sender is a buddy, not a client contact"
    assert len(buddies) == 1
    assert buddies[0].email == "topaz@recruitexpress.com.sg"
    assert "Topaz" in buddies[0].name
    assert buddies[0].email_domain == "recruitexpress.com.sg"
    assert referrals == 1


async def test_a_user_alias_is_not_a_buddy(agency) -> None:
    """If the original sender matches a declared user email alias, skip buddy capture.

    The user forwarding from their own work address is the user, not a buddy.
    """
    from app.services.ingest.persist import persist

    owner = await _a_recruiter(agency)
    message_id = uuid.uuid4()
    await _insert_message(agency, message_id, mailbox_owner_id=owner)

    # Declare an alias for the user.
    async with tenant_session(agency) as s:
        await s.execute(
            text(
                "INSERT INTO user_emails (id, tenant_id, user_id, email)"
                " VALUES (:id, :tid, :uid, :email)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": str(agency),
                "uid": str(owner),
                "email": "work@recruitexpress.com.sg",
            },
        )
        await s.commit()

    response, result, source = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(
        agency,
        message_id,
        response,
        result,
        source=source,
        original_sender_email="work@recruitexpress.com.sg",
        original_sender_name="The User",
    )

    async with tenant_session(agency) as s:
        buddies = (await s.execute(text("SELECT count(*) FROM buddies"))).scalar_one()
    assert buddies == 0, "the user's own alias must not create a buddy"


async def test_cf_and_of_in_the_email_set_the_sex_requirement_to_female(agency) -> None:
    """C/F and O/F in the source email set the opportunity's sex requirement.

    The client's coded preference (female, here) is written to
    `sex_requirement` at insert time, alongside an audit reason naming the
    codes. This is the field a recruiter reads in the modal — it must show the
    sex the client asked for, not stay on None.
    """
    from app.services.ingest.persist import persist

    await _seed_glossary(
        agency,
        [
            ("C/F", "Chinese, female"),
            ("O/F", "Any race, female"),
        ],
    )

    # The source contains both codes inside the evidence span so `_covers` is
    # satisfied for a single-vacancy email (every code belongs to the one job).
    source = "Registration Callers at The Learning Lab. C/F and O/F. $2000/month."
    company_at = source.index("The Learning Lab")
    title_at = source.index("Registration Callers")
    job = {
        "company": {
            "value": "The Learning Lab",
            "evidence": "The Learning Lab",
            "start_char": company_at,
            "end_char": company_at + len("The Learning Lab"),
            "confidence": 0.95,
        },
        "job_title": {
            "value": "Registration Callers",
            "evidence": "Registration Callers",
            "start_char": title_at,
            "end_char": title_at + len("Registration Callers"),
            "confidence": 0.95,
        },
    }
    response = ExtractionResponse.model_validate({"jobs": [job]})
    result = LLMResult(data={}, model="test/fast")

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        row = (
            await s.execute(
                text(
                    "SELECT sex_requirement, sex_requirement_reason FROM opportunities"
                    " WHERE email_message_id = :m"
                ),
                {"m": message_id},
            )
        ).one()
    assert row.sex_requirement == "female"
    assert row.sex_requirement_reason is not None
    assert "C/F" in row.sex_requirement_reason
    assert "O/F" in row.sex_requirement_reason


async def test_conflicting_sex_codes_leave_sex_requirement_unset(agency) -> None:
    """C/F and O/M together state both sexes — the row stays unset rather than
    guessing which role the client meant."""
    from app.services.ingest.persist import persist

    await _seed_glossary(
        agency,
        [
            ("C/F", "Chinese, female"),
            ("O/M", "Any race, male"),
        ],
    )

    source = "Role at Acme. C/F and O/M. $2000/month."
    company_at = source.index("Acme")
    title_at = source.index("Role")
    job = {
        "company": {
            "value": "Acme",
            "evidence": "Acme",
            "start_char": company_at,
            "end_char": company_at + len("Acme"),
            "confidence": 0.95,
        },
        "job_title": {
            "value": "Role",
            "evidence": "Role",
            "start_char": title_at,
            "end_char": title_at + len("Role"),
            "confidence": 0.95,
        },
    }
    response = ExtractionResponse.model_validate({"jobs": [job]})
    result = LLMResult(data={}, model="test/fast")

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        row = (
            await s.execute(
                text(
                    "SELECT sex_requirement, sex_requirement_reason FROM opportunities"
                    " WHERE email_message_id = :m"
                ),
                {"m": message_id},
            )
        ).one()
    assert row.sex_requirement is None
    assert row.sex_requirement_reason is None


async def _seed_glossary(tenant_id: uuid.UUID, codes: list[tuple[str, str]]) -> None:
    """Write glossary_codes rows directly — `detect()` reads these at ingest.

    Production seeds them on first read via the glossary API; tests reach the
    same table because persist's `_glossary()` is a plain SELECT, not the
    seeding endpoint. The normalised form mirrors what the seeder writes.
    """
    from app.services.ingest.glossary import normalise

    async with tenant_session(tenant_id) as s:
        for code, meaning in codes:
            await s.execute(
                text(
                    "INSERT INTO glossary_codes "
                    "(id, tenant_id, code, code_normalised, meaning, source)"
                    " VALUES (:id, :t, :c, :n, :m, 'agency')"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": str(tenant_id),
                    "c": code,
                    "n": normalise(code),
                    "m": meaning,
                },
            )
        await s.commit()


async def test_re_extracting_an_email_does_not_duplicate_its_codes(agency) -> None:
    """A retried extraction re-runs `detect()`, which is deterministic — it finds
    the same spans. Without the `ON CONFLICT` guard on `_INSERT_CODE`, each
    re-extraction would write a second row for every code at every offset. The
    opportunity insert is safe (`ON CONFLICT DO NOTHING`); the code insert must
    match it."""
    from app.services.ingest.persist import persist

    await _seed_glossary(agency, [("JD", "Job description")])

    source = "Registration Callers at The Learning Lab. JD enclosed. $2000/month."
    company_at = source.index("The Learning Lab")
    title_at = source.index("Registration Callers")
    job = {
        "company": {
            "value": "The Learning Lab",
            "evidence": "The Learning Lab",
            "start_char": company_at,
            "end_char": company_at + len("The Learning Lab"),
            "confidence": 0.95,
        },
        "job_title": {
            "value": "Registration Callers",
            "evidence": "Registration Callers",
            "start_char": title_at,
            "end_char": title_at + len("Registration Callers"),
            "confidence": 0.95,
        },
    }
    response = ExtractionResponse.model_validate({"jobs": [job]})
    result = LLMResult(data={}, model="test/fast")

    message_id = uuid.uuid4()
    await _insert_message(agency, message_id)
    # Re-extraction: the same email processed twice, exactly what a retry does.
    await persist(agency, message_id, response, result, source=source)
    await persist(agency, message_id, response, result, source=source)

    async with tenant_session(agency) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT code, start_char, end_char FROM opportunity_codes"
                    " WHERE opportunity_id ="
                    " (SELECT id FROM opportunities WHERE email_message_id = :m)"
                ),
                {"m": message_id},
            )
        ).all()
    # One JD at one offset — not two, despite two extractions.
    assert len(rows) == 1
    assert rows[0].code == "JD"

