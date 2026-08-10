"""A later email that changes a job order's requirements is a revision, not a
duplicate.

The client who originally asked for a female Chinese-only candidate emails
again: now open to male, all races. Two fates await that email in the old
code, both wrong — it lands in the same Graph conversation and is hidden as a
re-forward duplicate (the change silently vanishes), or it arrives in a new
thread and sits as a second open row with nothing linking it (the stale
requirements stay open forever).

This module pins the fix: ingestion detects the change, points the old open
row at the new one via `superseded_by_opportunity_id`, and the read paths —
list dedupe, detail load, and the matching entry points that read a job
order's requirements — follow the chain to the current revision.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user, sign_in

_SOURCE = "Vacancy at Acme Pte Ltd. Finance officer role, up to $3500 per month."
_NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def _extraction(
    company_name: str | None, requirements: str | None
) -> tuple[ExtractionResponse, LLMResult, str]:
    """One vacancy. `requirements` lets a test say what the client now asks for."""
    source = _SOURCE
    if requirements:
        source = f"{_SOURCE}\nRequirements: {requirements}"
    salary_at = source.index("up to $3500")
    period_at = source.index("per month")
    job: dict = {
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
    if company_name is not None:
        job["company"] = {
            "value": company_name,
            "evidence": "Acme Pte Ltd",
            "start_char": source.index("Acme Pte Ltd"),
            "end_char": source.index("Acme Pte Ltd") + len("Acme Pte Ltd"),
            "confidence": 0.9,
        }
    if requirements:
        job["requirements"] = {
            "value": requirements,
            "evidence": requirements,
            "start_char": source.index(requirements),
            "end_char": source.index(requirements) + len(requirements),
            "confidence": 0.9,
        }
    response = ExtractionResponse.model_validate({"jobs": [job]})
    return response, LLMResult(data={}, model="test/fast"), source


@pytest.fixture
def captured_events(monkeypatch) -> list:
    """What ingestion asked to be sent, without a notification catalogue."""
    events: list = []

    async def _capture(event, session) -> list:
        events.append(event)
        return []

    monkeypatch.setattr("app.services.ingest.persist.emit", _capture)
    return events


async def _mailbox_and_message(
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    *,
    sender_email: str,
    conversation_id: str | None,
    received: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
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
                " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
                " conversation_id, received_datetime)"
                " VALUES (:i, :t, :m, :g, 'Vacancy', :s, :c, :r)"
            ),
            {
                "i": message_id,
                "t": tenant_id,
                "m": mailbox_id,
                "g": f"MSG-{message_id.hex[:8]}",
                "s": sender_email,
                "c": conversation_id,
                "r": received or _NOW,
            },
        )
        await s.commit()
    return mailbox_id, message_id


async def _opportunity_row(opportunity_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, email_message_id, requirements,"
                    " superseded_by_opportunity_id, superseded_at"
                    " FROM opportunities WHERE id = :i"
                ),
                {"i": opportunity_id},
            )
        ).one()
        return row


async def _conversation_of(message_id: uuid.UUID) -> str | None:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT conversation_id FROM email_messages WHERE id = :i"),
                {"i": message_id},
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Write time: the change is detected and the old row is linked to the new one
# ---------------------------------------------------------------------------


async def test_a_changed_requirements_email_supersedes_the_old_open_row(
    captured_events,
) -> None:
    """Female/Chinese-only on day 1, open-to-all on day 5 — the later email is
    a revision, and the old open row points at it."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-1",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-1",
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)

        changed, result, source = _extraction("Acme Pte Ltd", "Open to male, all races")
        second_ids = await persist(tenant_id, second_message, changed, result, source=source)

        old_row = await _opportunity_row(first_ids[0])
        new_row = await _opportunity_row(second_ids[0])
        assert old_row.superseded_by_opportunity_id == new_row.id
        assert old_row.superseded_at is not None
        assert new_row.superseded_by_opportunity_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_identical_reforward_does_not_supersede(captured_events) -> None:
    """Same requirements again is a re-forward, not a revision — no link."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-2",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-2",
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)

        same, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        second_ids = await persist(tenant_id, second_message, same, result, source=source)

        old_row = await _opportunity_row(first_ids[0])
        new_row = await _opportunity_row(second_ids[0])
        assert old_row.superseded_by_opportunity_id is None
        assert new_row.superseded_by_opportunity_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_new_thread_from_the_same_client_supersedes_when_unique(
    captured_events,
) -> None:
    """A genuinely new email (new conversation) about the same vacancy still
    supersedes when it matches the same client + company/title/location.

    The two emails share a sender domain, so ingestion resolves both to the
    same client; the fallback in `_maybe_supersede` then links the old row.
    """
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-a",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-b",  # a different thread
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)

        changed, result, source = _extraction("Acme Pte Ltd", "Open to male, all races")
        second_ids = await persist(tenant_id, second_message, changed, result, source=source)

        old_row = await _opportunity_row(first_ids[0])
        new_row = await _opportunity_row(second_ids[0])
        assert old_row.superseded_by_opportunity_id == new_row.id
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_changed_email_does_not_supersede_a_placed_job(captured_events) -> None:
    """A role that has been placed is finished — a later change is new work,
    not a revision of the finished placement."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-3",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-3",
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)

        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE opportunities SET placement_type = 'local_hire' WHERE id = :i"),
                {"i": first_ids[0]},
            )
            await s.commit()

        changed, result, source = _extraction("Acme Pte Ltd", "Open to male, all races")
        second_ids = await persist(tenant_id, second_message, changed, result, source=source)

        old_row = await _opportunity_row(first_ids[0])
        new_row = await _opportunity_row(second_ids[0])
        assert old_row.superseded_by_opportunity_id is None
        assert new_row.superseded_by_opportunity_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_chain_of_revisions_links_each_step(captured_events) -> None:
    """Three emails, two changes: the chain A -> B -> C, each pointing at the next."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        ids = []
        for _i, requirements in enumerate(
            ["Female, Chinese only", "Open to male, all races", "Open to all nationalities"]
        ):
            _, message = await _mailbox_and_message(
                tenant_id, recruiter, sender_email="hr@acme.com.sg",
                conversation_id="conv-chain",
            )
            extraction, result, source = _extraction("Acme Pte Ltd", requirements)
            ids.extend(await persist(tenant_id, message, extraction, result, source=source))

        a, b, c = ids
        assert (await _opportunity_row(a)).superseded_by_opportunity_id == b
        assert (await _opportunity_row(b)).superseded_by_opportunity_id == c
        assert (await _opportunity_row(c)).superseded_by_opportunity_id is None
    finally:
        await cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Read time: the list shows the current revision, and loads resolve the chain
# ---------------------------------------------------------------------------


async def _supersede_pair(make_tenant, make_opportunity, *, same_conversation: bool = True):
    """An original open job order and a revision of it that replaced it.

    Returns (tenant_id, user_id, original_id, revision_id). The original's
    `superseded_by` points at the revision, exactly as `persist()` would leave
    it after a requirements-change email.
    """
    tenant_id, user_id, mailbox_id = await make_tenant("supersede-read")
    conv = "AAMkAAG-supersede-conv" if same_conversation else "AAMkAAG-original-conv"
    rev_conv = conv if same_conversation else "AAMkAAG-revision-conv"

    original = await make_opportunity(
        tenant_id, mailbox_id,
        received_datetime=_NOW,
        job_title_raw="Finance officer",
        company_name_raw="Acme Pte Ltd",
        requirements="Female, Chinese only",
    )
    revision = await make_opportunity(
        tenant_id, mailbox_id,
        received_datetime=_NOW.replace(day=12),
        job_title_raw="Finance officer",
        company_name_raw="Acme Pte Ltd",
        requirements="Open to male, all races",
    )
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE email_messages SET conversation_id = :c WHERE id = :m"),
            {"c": conv, "m": (await _email_of(original))},
        )
        await s.execute(
            text("UPDATE email_messages SET conversation_id = :c WHERE id = :m"),
            {"c": rev_conv, "m": (await _email_of(revision))},
        )
        await s.execute(
            text(
                "UPDATE opportunities SET superseded_by_opportunity_id = :rev,"
                " superseded_at = :at WHERE id = :orig"
            ),
            {"rev": revision, "at": _NOW.replace(day=12), "orig": original},
        )
        await s.commit()
    return tenant_id, user_id, original, revision


async def _email_of(opportunity_id: uuid.UUID) -> uuid.UUID:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT email_message_id FROM opportunities WHERE id = :i"),
                {"i": opportunity_id},
            )
        ).scalar_one()


async def test_the_list_shows_the_revision_and_hides_the_superseded_row(
    client, seeded,
) -> None:
    """The current revision is on the list; the row it replaced is not."""
    make_tenant, make_opportunity, _ = seeded
    tenant_id, user_id, original, revision = await _supersede_pair(
        make_tenant, make_opportunity
    )
    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?dedupe=true")).json()
    shown = {row["id"] for row in body["items"]}
    assert str(revision) in shown
    assert str(original) not in shown
    assert body["hidden"] == 1


async def test_the_list_marks_the_revision(client, seeded) -> None:
    """The revision row carries `revision_of_opportunity_id`, so the UI can say
    "requirements updated" instead of treating it as a fresh vacancy."""
    make_tenant, make_opportunity, _ = seeded
    tenant_id, user_id, original, revision = await _supersede_pair(
        make_tenant, make_opportunity
    )
    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?dedupe=true")).json()
    revision_row = next(row for row in body["items"] if row["id"] == str(revision))
    assert revision_row["revision_of_opportunity_id"] == str(original)
    assert revision_row["superseded_by_opportunity_id"] is None


async def test_without_dedupe_the_superseded_row_is_still_visible(client, seeded) -> None:
    """`dedupe=false` shows history — the recruiter can always see what the
    client originally asked for."""
    make_tenant, make_opportunity, _ = seeded
    tenant_id, user_id, original, revision = await _supersede_pair(
        make_tenant, make_opportunity
    )
    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?dedupe=false")).json()
    shown = {row["id"] for row in body["items"]}
    assert str(revision) in shown
    assert str(original) in shown
    assert body["hidden"] == 0


async def test_loading_a_superseded_id_returns_the_current_revision(
    client, seeded,
) -> None:
    """A stale notification or bookmark points at the old id; the detail must
    show the requirements the client *currently* stated, not the replaced ones."""
    make_tenant, make_opportunity, _ = seeded
    tenant_id, user_id, original, revision = await _supersede_pair(
        make_tenant, make_opportunity
    )
    sign_in(client, user_id, tenant_id)
    body = (await client.get(f"/api/opportunities/{original}")).json()
    assert body["id"] == str(revision)
    assert body["requirements"] == "Open to male, all races"


async def test_eligible_for_uses_the_current_revisions_requirements(
    client, seeded,
) -> None:
    """Matching must not be affected by stale requirements: `?eligible_for=` on
    the superseded id reads the revision that replaced it."""
    from app.db.rls import tenant_session
    from app.services.visibility import load_visible_opportunity

    make_tenant, make_opportunity, _ = seeded
    tenant_id, user_id, original, revision = await _supersede_pair(
        make_tenant, make_opportunity
    )

    async with tenant_session(tenant_id) as session:
        loaded = await load_visible_opportunity(session, original, user_id, "recruiter")
        assert loaded.id == revision
        assert loaded.requirements == "Open to male, all races"


async def test_the_visibility_loader_follows_a_chain(client, seeded) -> None:
    """A chain of two revisions resolves to the newest — matching reads the
    client's latest word, not the second-newest."""
    from app.db.rls import tenant_session
    from app.services.visibility import load_visible_opportunity

    make_tenant, make_opportunity, _ = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("supersede-chain-read")
    conv = "AAMkAAG-chain-conv"
    ids = []
    for day, requirements in enumerate(
        ["Female, Chinese only", "Open to male, all races", "Open to all nationalities"], start=1
    ):
        opp = await make_opportunity(
            tenant_id, mailbox_id,
            received_datetime=_NOW.replace(day=day),
            job_title_raw="Finance officer",
            company_name_raw="Acme Pte Ltd",
            requirements=requirements,
        )
        ids.append(opp)
    a, b, c = ids
    async with AdminSessionLocal() as s:
        for message_id in [await _email_of(a), await _email_of(b), await _email_of(c)]:
            await s.execute(
                text("UPDATE email_messages SET conversation_id = :c WHERE id = :m"),
                {"c": conv, "m": message_id},
            )
        await s.execute(
            text(
                "UPDATE opportunities SET superseded_by_opportunity_id = :rev,"
                " superseded_at = :at WHERE id = :orig"
            ),
            {"rev": b, "at": _NOW.replace(day=2), "orig": a},
        )
        await s.execute(
            text(
                "UPDATE opportunities SET superseded_by_opportunity_id = :rev,"
                " superseded_at = :at WHERE id = :orig"
            ),
            {"rev": c, "at": _NOW.replace(day=3), "orig": b},
        )
        await s.commit()

    async with tenant_session(tenant_id) as session:
        loaded = await load_visible_opportunity(session, a, user_id, "recruiter")
        assert loaded.id == c

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?dedupe=true")).json()
    shown = {row["id"] for row in body["items"]}
    assert str(c) in shown
    assert str(a) not in shown
    assert str(b) not in shown


async def test_a_different_role_in_the_same_thread_does_not_supersede(
    captured_events,
) -> None:
    """M1 regression: a follow-up about a *different* vacancy in the same
    conversation must never supersede a live job order."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-m1",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-m1",  # same thread, but a different role
        )

        finance, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        finance_ids = await persist(tenant_id, first_message, finance, result, source=source)

        # The follow-up is for a Driver role — same company, different title.
        driver_source = "Vacancy at Acme Pte Ltd. Driver role, up to $2500 per month."
        driver_job = {
            "job_title": {
                "value": "Driver",
                "evidence": "Driver",
                "start_char": driver_source.index("Driver"),
                "end_char": driver_source.index("Driver") + len("Driver"),
                "confidence": 0.95,
            },
            "company": {
                "value": "Acme Pte Ltd",
                "evidence": "Acme Pte Ltd",
                "start_char": driver_source.index("Acme Pte Ltd"),
                "end_char": driver_source.index("Acme Pte Ltd") + len("Acme Pte Ltd"),
                "confidence": 0.9,
            },
            "salary": {
                "value": "2500",
                "evidence": "up to $2500",
                "start_char": driver_source.index("up to $2500"),
                "end_char": driver_source.index("up to $2500") + len("up to $2500"),
                "confidence": 0.9,
            },
            "salary_period": {
                "value": "month",
                "evidence": "per month",
                "start_char": driver_source.index("per month"),
                "end_char": driver_source.index("per month") + len("per month"),
                "confidence": 0.9,
            },
        }
        driver = ExtractionResponse.model_validate({"jobs": [driver_job]})
        driver_ids = await persist(tenant_id, second_message, driver, result, source=driver_source)

        finance_row = await _opportunity_row(finance_ids[0])
        driver_row = await _opportunity_row(driver_ids[0])
        assert finance_row.superseded_by_opportunity_id is None
        assert driver_row.superseded_by_opportunity_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_revision_carries_the_assignee_onto_the_successor(
    captured_events,
) -> None:
    """M3 regression: a revision of an *assigned* job order keeps the recruiter
    who was working it — the claim must not silently fall back to the queue."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-m3",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-m3",
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)

        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE opportunities SET assigned_user_id = :u WHERE id = :i"),
                {"u": recruiter, "i": first_ids[0]},
            )
            await s.commit()

        changed, result, source = _extraction("Acme Pte Ltd", "Open to male, all races")
        second_ids = await persist(tenant_id, second_message, changed, result, source=source)

        new_row = await _opportunity_row(second_ids[0])
        async with AdminSessionLocal() as s:
            assigned = (
                await s.execute(
                    text("SELECT assigned_user_id FROM opportunities WHERE id = :i"),
                    {"i": second_ids[0]},
                )
            ).scalar_one()
        assert assigned == recruiter
        assert new_row.superseded_by_opportunity_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_identical_copy_in_a_new_thread_is_hidden_by_default(
    client, captured_events,
) -> None:
    """A duplicate that arrives as a brand-new email (different conversation)
    must not show as a second open row — the read-time dedupe only collapses
    same-conversation re-forwards, so the write path links the copy to the
    canonical row."""
    from app.db.rls import tenant_session
    from app.services.ingest.persist import persist
    from app.services.visibility import load_visible_opportunity

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-dup-a",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-dup-b",  # a different thread, same content
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)

        same, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        second_ids = await persist(tenant_id, second_message, same, result, source=source)

        old_row = await _opportunity_row(first_ids[0])
        new_row = await _opportunity_row(second_ids[0])
        # The copy is linked to the canonical row, not the other way round.
        assert new_row.superseded_by_opportunity_id == old_row.id
        assert old_row.superseded_by_opportunity_id is None

        # The loader resolves the copy to the canonical row.
        async with tenant_session(tenant_id) as session:
            loaded = await load_visible_opportunity(
                session, second_ids[0], recruiter, "recruiter"
            )
            assert loaded.id == first_ids[0]
    finally:
        await cleanup_tenant(tenant_id)


async def test_the_list_hides_a_cross_conversation_duplicate_by_default(
    client, captured_events,
) -> None:
    """Default list shows one row for an identical job order, even when it
    arrived twice in two different conversations."""
    from app.services.ingest.persist import persist

    tenant_id, recruiter = await seed_tenant_with_user()
    try:
        _, first_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-dup-c",
        )
        _, second_message = await _mailbox_and_message(
            tenant_id, recruiter, sender_email="hr@acme.com.sg",
            conversation_id="conv-dup-d",
        )

        first, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        first_ids = await persist(tenant_id, first_message, first, result, source=source)
        same, result, source = _extraction("Acme Pte Ltd", "Female, Chinese only")
        second_ids = await persist(tenant_id, second_message, same, result, source=source)

        sign_in(client, recruiter, tenant_id)
        body = (await client.get("/api/opportunities?dedupe=true")).json()
        shown = {row["id"] for row in body["items"]}
        assert str(first_ids[0]) in shown
        assert str(second_ids[0]) not in shown
        assert body["hidden"] >= 1
    finally:
        await cleanup_tenant(tenant_id)
