"""Delta synchronisation and reconciliation (plan §9).

This is the recovery path. Webhooks are fast but lossy — a missed
notification, a webhook outage, a Graph incident — and this walk is what makes
any of that survivable. It is also the backfill path; onboarding differs only
in where the walk starts.

The distinction this module exists to preserve: Graph's message delta is
**folder-scoped**, so an `@removed` event usually means the recruiter filed the
mail into a subfolder, not that it was deleted. Treating those the same would
invalidate opportunities extracted from mail that is still sitting in the
mailbox.

allow-hardcode: the SQL and Graph payloads below are test fixtures.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.services.graph import delta as delta_module
from app.services.graph.client import GraphClient
from app.services.graph.delta import backfill_mailbox, sync_mailbox

DELTA_LINK = "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=next"


@pytest.fixture
async def mailbox(admin_session):
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months,"
            "  initial_sync_from)"
            " VALUES (:id, :tenant, 'ms-user', 'jobs-folder', 'folder', 24,"
            "         now() - interval '3 days')"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


@pytest.fixture
def queued(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        calls.append((name, kwargs))
        return True

    monkeypatch.setattr(delta_module, "enqueue", _enqueue)
    return calls


def _pages(*payloads):
    """Serve each payload in turn, recording the URLs requested."""
    remaining = list(payloads)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if not remaining:
            raise AssertionError(f"unexpected extra request to {request.url}")
        payload = remaining.pop(0)
        if isinstance(payload, int):
            return httpx.Response(payload, json={})
        return httpx.Response(200, json=payload)

    return handler, requested


def _graph(handler) -> GraphClient:
    return GraphClient(token="t", transport=httpx.MockTransport(handler))


async def _rows(tenant_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT graph_message_id, processing_status, source_state"
                    " FROM email_messages ORDER BY graph_message_id"
                )
            )
        ).all()


async def _delta_link(tenant_id, mailbox_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT delta_link FROM mailboxes WHERE id = :id"),
                {"id": mailbox_id},
            )
        ).scalar_one()


# --- the distinction this module exists for ---------------------------------


async def test_a_message_filed_elsewhere_is_not_treated_as_deleted(
    admin_session, mailbox, queued
):
    """Graph's message delta is folder-scoped, so `@removed` usually means the
    recruiter moved it. The body is already safe in R2 and the vacancy it
    described is still real — invalidating it would lose a live job order.
    """
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
            "  classification_status)"
            " VALUES (:id, :tenant, :mailbox, 'MSG-MOVED', 'extracted', 'recruitment')"
        ),
        {"id": uuid.uuid4(), "tenant": tenant_id, "mailbox": mailbox_id},
    )
    await admin_session.commit()

    handler, _ = _pages(
        {
            "value": [{"id": "MSG-MOVED", "@removed": {"reason": "changed"}}],
            "@odata.deltaLink": DELTA_LINK,
        }
    )
    await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    row = (await _rows(tenant_id))[0]
    assert row.source_state == "removed_from_folder"
    assert row.processing_status == "extracted", "the opportunity remains valid"


async def test_a_removal_never_queues_work(admin_session, mailbox, queued):
    tenant_id, mailbox_id = mailbox
    handler, _ = _pages(
        {
            "value": [{"id": "MSG-UNKNOWN", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": DELTA_LINK,
        }
    )

    await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert queued == []


# --- ordinary reconciliation ------------------------------------------------


async def test_new_messages_are_recorded_and_queued(mailbox, queued):
    tenant_id, mailbox_id = mailbox
    handler, _ = _pages(
        {
            "value": [{"id": "NEW-1"}, {"id": "NEW-2"}],
            "@odata.deltaLink": DELTA_LINK,
        }
    )

    result = await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert result.recorded == 2
    assert len(await _rows(tenant_id)) == 2
    assert [name for name, _ in queued] == ["fetch_email", "fetch_email"]
    # Every job carries its tenant — see the plan's Global Constraints.
    for _, kwargs in queued:
        assert kwargs["tenant_id"] == str(tenant_id)
        assert kwargs["mailbox_id"] == str(mailbox_id)


async def test_the_checkpoint_is_stored_for_the_next_sweep(mailbox, queued):
    tenant_id, mailbox_id = mailbox
    handler, _ = _pages({"value": [{"id": "NEW-1"}], "@odata.deltaLink": DELTA_LINK})

    await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert await _delta_link(tenant_id, mailbox_id) == DELTA_LINK


async def test_a_stored_checkpoint_is_used_instead_of_starting_over(
    admin_session, mailbox, queued
):
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text("UPDATE mailboxes SET delta_link = :link WHERE id = :id"),
        {"link": DELTA_LINK, "id": mailbox_id},
    )
    await admin_session.commit()

    handler, requested = _pages({"value": [], "@odata.deltaLink": DELTA_LINK})
    await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert requested == [DELTA_LINK], "the walk resumes rather than re-reading"


async def test_replaying_a_page_creates_no_duplicates(mailbox, queued):
    """The webhook, this sweep and the backfill all legitimately see the same
    message. Replay has to be free."""
    tenant_id, mailbox_id = mailbox
    page = {"value": [{"id": "SAME"}], "@odata.deltaLink": DELTA_LINK}

    for _ in range(2):
        handler, _ = _pages(page)
        await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert len(await _rows(tenant_id)) == 1
    assert len(queued) == 1, "only the first sighting is new work"


async def test_pagination_follows_the_absolute_next_link(mailbox, queued):
    """Graph hands back a full URL for page two, not a path."""
    tenant_id, mailbox_id = mailbox
    page_two = "https://graph.microsoft.com/v1.0/me/messages/delta?$skiptoken=p2"
    handler, requested = _pages(
        {"value": [{"id": "P1"}], "@odata.nextLink": page_two},
        {"value": [{"id": "P2"}], "@odata.deltaLink": DELTA_LINK},
    )

    result = await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert result.recorded == 2
    assert requested[1] == page_two
    assert await _delta_link(tenant_id, mailbox_id) == DELTA_LINK


# --- an expired checkpoint --------------------------------------------------


async def test_an_expired_checkpoint_restarts_the_walk(admin_session, mailbox, queued):
    """Graph answers 410 when a deltaLink has aged out.

    That is a recovery instruction, not a failure: drop the token and re-walk.
    Leaving it stored would make every future sweep fail the same way, and the
    recovery layer would be permanently dead while looking merely quiet.
    """
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text("UPDATE mailboxes SET delta_link = :link WHERE id = :id"),
        {"link": "https://graph.microsoft.com/v1.0/stale", "id": mailbox_id},
    )
    await admin_session.commit()

    handler, requested = _pages(
        410,
        {"value": [{"id": "AFTER-RESYNC"}], "@odata.deltaLink": DELTA_LINK},
    )

    result = await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert result.recorded == 1
    assert requested[0].endswith("/stale")
    assert "/messages/delta" in requested[1], "restarted from the folder, not the token"
    assert await _delta_link(tenant_id, mailbox_id) == DELTA_LINK


async def test_an_expired_checkpoint_is_not_retried_forever(
    admin_session, mailbox, queued
):
    """If the restart also 410s, give up for this sweep rather than looping."""
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text("UPDATE mailboxes SET delta_link = :link WHERE id = :id"),
        {"link": "https://graph.microsoft.com/v1.0/stale", "id": mailbox_id},
    )
    await admin_session.commit()

    handler, requested = _pages(410, 410)

    from app.services.graph.client import GraphResyncRequired

    with pytest.raises(GraphResyncRequired):
        await sync_mailbox(tenant_id, mailbox_id, _graph(handler))

    assert len(requested) == 2, "one restart, not a loop"


# --- bounded backfill -------------------------------------------------------


async def test_the_cap_never_skips_the_rest_of_a_page(mailbox, queued):
    """The cap is applied at page boundaries, not mid-page.

    Stopping mid-page and resuming from `@odata.nextLink` would skip every
    item after the cap position — silently and permanently, on the code path
    whose entire purpose is not losing mail. Overshooting by at most one page
    is the cheaper error by a wide margin.
    """
    tenant_id, mailbox_id = mailbox
    page_two = "https://graph.microsoft.com/v1.0/page2"
    handler, _ = _pages(
        {"value": [{"id": f"M-{n}"} for n in range(5)], "@odata.nextLink": page_two}
    )

    result = await sync_mailbox(tenant_id, mailbox_id, _graph(handler), max_messages=3)

    assert result.capped is True
    assert result.recorded == 5, "the page is finished, not abandoned"
    stored = [row.graph_message_id for row in await _rows(tenant_id)]
    assert stored == [f"M-{n}" for n in range(5)], "no message on the page is lost"


async def test_a_capped_walk_stores_where_it_stopped(mailbox, queued):
    """Without a checkpoint the next sweep re-walks from the beginning and
    re-caps at the same place — forever, never reaching the newest mail."""
    tenant_id, mailbox_id = mailbox
    page_two = "https://graph.microsoft.com/v1.0/page2"
    handler, _ = _pages(
        {"value": [{"id": f"M-{n}"} for n in range(5)], "@odata.nextLink": page_two}
    )

    await sync_mailbox(tenant_id, mailbox_id, _graph(handler), max_messages=3)

    assert await _delta_link(tenant_id, mailbox_id) == page_two


async def test_landing_exactly_on_the_cap_at_the_end_is_not_truncation(
    mailbox, queued
):
    """Reaching the cap on the final page means the walk finished.

    Reporting that as capped would tell the user their history was cut short
    when there was nothing left to fetch — and would store a resume link for a
    walk that has no remainder.
    """
    tenant_id, mailbox_id = mailbox
    handler, _ = _pages(
        {"value": [{"id": f"E-{n}"} for n in range(3)], "@odata.deltaLink": DELTA_LINK}
    )

    result = await sync_mailbox(tenant_id, mailbox_id, _graph(handler), max_messages=3)

    assert result.recorded == 3
    assert result.capped is False, "the folder ended; the cap was incidental"
    assert await _delta_link(tenant_id, mailbox_id) == DELTA_LINK


async def test_already_known_messages_do_not_consume_the_cap(
    admin_session, mailbox, queued
):
    """A resumed walk would otherwise spend its whole budget re-reading what it
    already has, and never reach the mail it stopped short of."""
    tenant_id, mailbox_id = mailbox
    for n in range(3):
        await admin_session.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id)"
                " VALUES (:id, :tenant, :mailbox, :graph_id)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "graph_id": f"K-{n}",
            },
        )
    await admin_session.commit()

    handler, _ = _pages(
        {
            "value": [{"id": f"K-{n}"} for n in range(3)] + [{"id": "NEW-1"}],
            "@odata.deltaLink": DELTA_LINK,
        }
    )

    result = await sync_mailbox(tenant_id, mailbox_id, _graph(handler), max_messages=2)

    assert result.seen == 4
    assert result.recorded == 1, "only the unseen message counts"
    assert result.capped is False, "one new message is under a cap of two"


async def test_the_backfill_filters_from_the_chosen_start_and_records_completion(
    mailbox, queued
):
    tenant_id, mailbox_id = mailbox
    since = datetime.now(UTC) - timedelta(days=3)
    handler, requested = _pages({"value": [{"id": "OLD-1"}], "@odata.deltaLink": DELTA_LINK})

    await backfill_mailbox(tenant_id, mailbox_id, _graph(handler), since)

    assert "receivedDateTime" in requested[0]
    async with tenant_session(tenant_id) as session:
        completed = (
            await session.execute(
                text("SELECT backfill_completed_at FROM mailboxes WHERE id = :id"),
                {"id": mailbox_id},
            )
        ).scalar_one()
    assert completed is not None


async def test_the_backfill_is_bounded_by_the_configured_cap(
    monkeypatch, mailbox, queued
):
    """The cap is read from configuration, not hardcoded in the backfill.

    Patched down to a small number: proving the setting is honoured does not
    require inserting five thousand rows, and a test that slow is one nobody
    runs.
    """
    tenant_id, mailbox_id = mailbox
    monkeypatch.setattr(settings, "INITIAL_SYNC_MAX_MESSAGES", 4)
    # Two pages: the first exceeds the cap, so the second must never be
    # requested. `_pages` raises on an unexpected request, which is what proves
    # the walk actually stopped rather than merely counting to four.
    handler, requested = _pages(
        {
            "value": [{"id": f"B-{n}"} for n in range(5)],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
        }
    )

    result = await backfill_mailbox(
        tenant_id, mailbox_id, _graph(handler), datetime.now(UTC) - timedelta(days=1)
    )

    assert result.capped is True
    assert result.recorded == 5, "the page is finished, then the walk stops"
    assert len(requested) == 1, "page two is never fetched"
    assert len(await _rows(tenant_id)) == 5
