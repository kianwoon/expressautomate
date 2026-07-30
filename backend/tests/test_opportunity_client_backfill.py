"""The `opportunities.client_id` backfill must refuse to guess.

`client_mentions` is an evidence trail — one email legitimately names many
clients — so the migration only assigns a client when the evidence points one
way. Production data is the reason: most matched messages there mention six
distinct clients. Assigning an arbitrary one of them routes a job order to the
wrong recruiter.

The migration's SQL is imported from the revision file itself, so this pins the
statement that actually runs rather than a copy of it.

allow-hardcode: SQL statements and fixture names, not a phrase list.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.conftest import AdminSessionLocal

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260730_1636_opportunity_assignment.py"
)


def _backfill_sql() -> str:
    spec = importlib.util.spec_from_file_location("_opportunity_assignment", _REVISION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CLIENT_BACKFILL_SQL


@pytest.fixture
async def production_shaped_agency():
    """One email mentioning six clients, one mentioning exactly one.

    Both carry an opportunity with `client_id` cleared, which is the state the
    migration finds the table in.
    """
    tid, mid = uuid.uuid4(), uuid.uuid4()
    ambiguous_email, clear_email = uuid.uuid4(), uuid.uuid4()
    ambiguous_opp, clear_opp = uuid.uuid4(), uuid.uuid4()
    lone_client = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :sl)"),
            {"i": tid, "sl": f"a-{tid.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope,"
                " status, retention_months) VALUES (:i, :t, 'u', 'f', 'folder',"
                " 'active', 24)"
            ),
            {"i": mid, "t": tid},
        )
        for eid in (ambiguous_email, clear_email):
            await s.execute(
                text(
                    "INSERT INTO email_messages (id, tenant_id, mailbox_id,"
                    " graph_message_id, processing_status, source_state,"
                    " classification_status) VALUES (:i, :t, :m, :g, 'fetched',"
                    " 'present', 'recruitment')"
                ),
                {"i": eid, "t": tid, "m": mid, "g": f"MSG-{eid.hex[:8]}"},
            )
        # Six distinct clients on one email, as production has them: a mix of
        # 'name' and 'email_domain', so narrowing by `matched_by` would not
        # break the tie either.
        for n in range(6):
            cid = uuid.uuid4()
            await s.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, name, name_normalized, status)"
                    " VALUES (:i, :t, :n, :nn, 'unconfirmed')"
                ),
                {"i": cid, "t": tid, "n": f"Client {n}", "nn": f"client {n}"},
            )
            await s.execute(
                text(
                    "INSERT INTO client_mentions (id, tenant_id, client_id,"
                    " email_message_id, matched_by) VALUES (:i, :t, :c, :e, :mb)"
                ),
                {
                    "i": uuid.uuid4(),
                    "t": tid,
                    "c": cid,
                    "e": ambiguous_email,
                    "mb": "email_domain" if n % 2 else "name",
                },
            )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status)"
                " VALUES (:i, :t, 'Sole', 'sole', 'unconfirmed')"
            ),
            {"i": lone_client, "t": tid},
        )
        # Two mention rows, one client: repeated evidence is still one answer.
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id,"
                " email_message_id, matched_by) VALUES (:i, :t, :c, :e, 'name')"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": lone_client, "e": clear_email},
        )
        for opp, eid in ((ambiguous_opp, ambiguous_email), (clear_opp, clear_email)):
            await s.execute(
                text(
                    "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                    " job_title_raw, review_status, quality_state, client_id)"
                    " VALUES (:i, :t, :e, 'Analyst', 'ready', 'likely', NULL)"
                ),
                {"i": opp, "t": tid, "e": eid},
            )
        await s.commit()

    yield tid, ambiguous_opp, clear_opp, lone_client

    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
        await s.commit()


async def _client_ids(*opportunity_ids: uuid.UUID) -> list[uuid.UUID | None]:
    async with AdminSessionLocal() as s:
        return [
            (
                await s.execute(
                    text("SELECT client_id FROM opportunities WHERE id = :i"),
                    {"i": opp},
                )
            ).scalar_one()
            for opp in opportunity_ids
        ]


async def test_backfill_assigns_only_the_unambiguous_email(production_shaped_agency):
    tid, ambiguous_opp, clear_opp, lone_client = production_shaped_agency
    async with AdminSessionLocal() as s:
        await s.execute(text(_backfill_sql()))
        await s.commit()

    ambiguous, clear = await _client_ids(ambiguous_opp, clear_opp)
    assert ambiguous is None, "six candidate clients must not be guessed between"
    assert clear == lone_client


async def test_backfill_ignores_another_tenants_mention_of_the_same_email(
    production_shaped_agency,
):
    """Another agency's evidence neither supplies an answer nor creates a tie."""
    tid, _ambiguous_opp, clear_opp, lone_client = production_shaped_agency
    other_tid, other_client = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        email_id = (
            await s.execute(
                text("SELECT email_message_id FROM opportunities WHERE id = :i"),
                {"i": clear_opp},
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'B', :sl)"),
            {"i": other_tid, "sl": f"b-{other_tid.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status)"
                " VALUES (:i, :t, 'Other', 'other', 'unconfirmed')"
            ),
            {"i": other_client, "t": other_tid},
        )
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id,"
                " email_message_id, matched_by) VALUES (:i, :t, :c, :e, 'name')"
            ),
            {"i": uuid.uuid4(), "t": other_tid, "c": other_client, "e": email_id},
        )
        await s.execute(text(_backfill_sql()))
        await s.commit()

    try:
        (clear,) = await _client_ids(clear_opp)
        assert clear == lone_client
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": other_tid})
            await s.commit()
