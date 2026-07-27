"""The extraction tables, and the isolation on them.

`opportunities` is the most sensitive table in the product — one agency's live
vacancies — and `extraction_evidence` quotes the source email verbatim. A
missing policy here would not fail; it would work, and show Agency A's roles to
Agency B.

allow-hardcode: SQL statements, not a phrase list.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session


@pytest.fixture
async def email_row(admin_session):
    tid, mid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " processing_status, source_state, classification_status)"
            " VALUES (:i, :t, :m, 'MSG', 'fetched', 'present', 'recruitment')"
        ),
        {"i": eid, "t": tid, "m": mid},
    )
    await admin_session.commit()
    yield tid, eid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


async def test_one_email_can_carry_several_opportunities(admin_session, email_row):
    """Plan §16: three vacancies in one email are three rows, not one."""
    tid, eid = email_row
    for title in ("Finance Officer", "Contract Accountant", "QA Executive"):
        await admin_session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_raw, review_status, quality_state)"
                " VALUES (:i, :t, :e, :title, 'ready', 'likely')"
            ),
            {"i": uuid.uuid4(), "t": tid, "e": eid, "title": title},
        )
    await admin_session.commit()

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM opportunities WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 3


async def test_salary_period_is_stored_alongside_the_amount(admin_session, email_row):
    """SGD 6,000 is meaningless for analytics without knowing per what."""
    tid, eid = email_row
    await admin_session.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, email_message_id, job_title_raw,"
            " salary_min, salary_max, salary_currency, salary_period, salary_raw,"
            " review_status, quality_state) VALUES (:i, :t, :e, 'Treasury', 5000, 7000,"
            " 'SGD', 'month', '$5,000-$7,000', 'ready', 'verified')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    row = (
        await admin_session.execute(
            text(
                "SELECT salary_period, salary_currency FROM opportunities"
                " WHERE email_message_id = :e"
            ),
            {"e": eid},
        )
    ).one()
    assert row.salary_period == "month"
    assert row.salary_currency == "SGD"


async def test_extractions_are_keyed_on_the_email_not_the_opportunity(
    admin_session, email_row
):
    """A run that finds nothing must still be recorded."""
    tid, eid = email_row
    await admin_session.execute(
        text(
            "INSERT INTO extractions (id, tenant_id, email_message_id, model_name,"
            " prompt_version, raw_response) VALUES (:i, :t, :e, 'test-model', 'v1', '{}')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM extractions WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 1


async def test_opportunities_are_tenant_isolated(admin_session, email_row):
    tid, eid = email_row
    await admin_session.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, email_message_id, job_title_raw,"
            " review_status, quality_state) VALUES (:i, :t, :e, 'Secret', 'ready', 'likely')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    async with tenant_session(uuid.uuid4()) as other:
        visible = (
            await other.execute(text("SELECT count(*) FROM opportunities"))
        ).scalar_one()
    assert visible == 0
