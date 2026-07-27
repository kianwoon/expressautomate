"""The sync-activity log behind the dashboard's "Recent sync activity" panel.

Four things this has to get right, and they fail in different directions:

- Agency A must not read Agency B's sync history. A mailbox that syncs at 3am
  every night is a fact about a competitor's operation, so this table is behind
  the same forced policy as the mail itself.
- A real sync must actually leave a row. The panel is worth nothing if the
  pipeline runs silently past it.
- Writing the row must never break the job it describes. This log covers the
  recovery jobs; a logging failure that aborts a sync turns the audit trail
  into the outage.
- The table must stay bounded. A row per sweep per mailbox forever is a slow
  leak that nothing surfaces until the disk does.

allow-hardcode: the SQL below is test fixture data.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models.sync_event import (
    KIND_DELTA_SYNC,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
)
from app.services.graph import delta as delta_module
from app.services.graph.delta import DeltaResult
from app.workers import jobs


@pytest.fixture(autouse=True)
def settings_the_suite_supplies(monkeypatch) -> None:
    """CI has no `.env`, so the suite states every value it depends on.

    Unconditional rather than a fallback: reading the developer machine's
    `.env` would test one retention bound locally and a different one in CI,
    and the trim is the assertion.
    """
    monkeypatch.setattr(settings, "SYNC_ACTIVITY_KEEP_PER_MAILBOX", 3)
    monkeypatch.setattr(settings, "SYNC_ACTIVITY_PAGE_LIMIT", 50)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """ASGI transport, not TestClient: TestClient drives its own event loop and
    the engine in app.db.session is pinned to the session-scoped one."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
async def agencies(admin_session):
    """Two agencies, each with a user and a mailbox.

    Seeded through the admin role because RLS is the thing under test: fixtures
    written through the restricted role would prove isolation by never having
    inserted the other tenant's rows at all.
    """
    made: list[uuid.UUID] = []

    async def make(slug: str):
        tenant_id, user_id, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await admin_session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :slug)"),
            {"id": tenant_id, "n": slug, "slug": f"{slug}-{tenant_id.hex[:8]}"},
        )
        await admin_session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role)"
                " VALUES (:id, :t, :e, 'member')"
            ),
            {"id": user_id, "t": tenant_id, "e": f"{tenant_id.hex[:8]}@{slug}.sg"},
        )
        await admin_session.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, folder_id, scope,"
                "  retention_months)"
                " VALUES (:id, :t, :u, :m, 'inbox', 'user', :r)"
            ),
            {
                "id": mailbox_id,
                "t": tenant_id,
                "u": user_id,
                "m": f"oid-{tenant_id.hex[:8]}",
                "r": settings.DEFAULT_RETENTION_MONTHS,
            },
        )
        await admin_session.commit()
        made.append(tenant_id)
        return tenant_id, user_id, mailbox_id

    yield make

    for tenant_id in made:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
    await admin_session.commit()


def sign_in(client: httpx.AsyncClient, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """The cookie the OAuth callback would have set, without the OAuth."""
    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )


async def _events(tenant_id: uuid.UUID):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT kind, outcome, detail FROM sync_events"
                    " ORDER BY created_at, id"
                )
            )
        ).all()


def _wire_sync(monkeypatch, result=None, error=None):
    """A delta sync that reaches no network and ends however the test says."""

    class _Client:
        async def aclose(self):
            return None

    async def _client(tenant_id, mailbox_id):
        return _Client()

    async def _sync(tenant_id, mailbox_id, client):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _client)
    monkeypatch.setattr(delta_module, "sync_mailbox", _sync)


# --- the security property --------------------------------------------------


async def test_the_table_is_behind_a_forced_row_policy(admin_session) -> None:
    """ENABLE alone is not enough, and the difference is invisible.

    Without FORCE the table owner bypasses the policy — and the owner is who
    migrations and any superuser session connect as — so an ENABLE-only table
    reads as protected in the catalogue while leaking to exactly the roles most
    likely to query it by hand.
    """
    enabled, forced = (
        await admin_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                " WHERE relname = 'sync_events'"
            )
        )
    ).one()
    assert enabled and forced

    policies = (
        await admin_session.execute(
            text("SELECT polname FROM pg_policy p"
                 " JOIN pg_class c ON c.oid = p.polrelid"
                 " WHERE c.relname = 'sync_events'")
        )
    ).scalars().all()
    assert "tenant_isolation" in policies


async def test_one_agency_never_reads_another_agencys_sync_history(
    client, agencies
) -> None:
    """The panel says when a mailbox last synced. That is a picture of another
    agency's operation even with no email body anywhere near it."""
    tenant_a, user_a, mailbox_a = await agencies("agency-a")
    tenant_b, _user_b, mailbox_b = await agencies("agency-b")
    await jobs.record_sync_event(
        tenant_a, mailbox_a, KIND_DELTA_SYNC, OUTCOME_SUCCEEDED, "Ours"
    )
    await jobs.record_sync_event(
        tenant_b, mailbox_b, KIND_DELTA_SYNC, OUTCOME_SUCCEEDED, "Theirs"
    )

    sign_in(client, user_a, tenant_a)
    body = (await client.get("/api/mailbox/activity")).json()

    assert [event["detail"] for event in body["events"]] == ["Ours"]


async def test_the_endpoint_refuses_anyone_who_is_not_signed_in(client) -> None:
    """No cookie, no history. Otherwise the panel is an unauthenticated report
    on when every agency in the deployment last received mail."""
    response = await client.get("/api/mailbox/activity")
    assert response.status_code == 401


# --- what actually gets recorded --------------------------------------------


async def test_a_sync_that_imported_email_leaves_a_row(monkeypatch, agencies) -> None:
    """The panel is worth nothing if the pipeline runs silently past it."""
    tenant_id, _user_id, mailbox_id = await agencies("agency-a")
    _wire_sync(monkeypatch, result=DeltaResult(seen=9, recorded=4, capped=False))

    await jobs.delta_sync_mailbox(
        None, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    rows = await _events(tenant_id)
    assert [(r.kind, r.outcome) for r in rows] == [(KIND_DELTA_SYNC, OUTCOME_SUCCEEDED)]
    assert "4" in rows[0].detail


async def test_an_empty_poll_leaves_nothing(monkeypatch, agencies) -> None:
    """This sweep runs every ten minutes for every active mailbox and most
    polls find nothing. A row per poll would push a genuine failure off the
    panel within the hour — the wall of "nothing happened" that makes the one
    line that matters invisible."""
    tenant_id, _user_id, mailbox_id = await agencies("agency-a")
    _wire_sync(monkeypatch, result=DeltaResult(seen=12, recorded=0, capped=False))

    await jobs.delta_sync_mailbox(
        None, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    assert await _events(tenant_id) == []


async def test_a_failed_sync_is_always_recorded(monkeypatch, agencies) -> None:
    """A failure is never routine, however often the sweep runs — and the job
    still raises, so arq's retry is untouched by the logging."""
    tenant_id, _user_id, mailbox_id = await agencies("agency-a")
    _wire_sync(monkeypatch, error=RuntimeError("graph exploded"))

    with pytest.raises(RuntimeError):
        await jobs.delta_sync_mailbox(
            None, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
        )

    rows = await _events(tenant_id)
    assert [(r.kind, r.outcome) for r in rows] == [(KIND_DELTA_SYNC, OUTCOME_FAILED)]


async def test_a_broken_audit_trail_cannot_break_the_sync(
    monkeypatch, agencies
) -> None:
    """The failure mode this guards against is circular: the log covers the
    recovery jobs, so a raising INSERT would abort a sync that had already
    succeeded, and arq would re-walk Graph every ten minutes to fix a diary."""
    tenant_id, _user_id, mailbox_id = await agencies("agency-a")
    _wire_sync(monkeypatch, result=DeltaResult(seen=3, recorded=3, capped=False))

    def _explode(*args, **kwargs):
        raise RuntimeError("the events table is gone")

    monkeypatch.setattr(jobs, "tenant_session", _explode)

    # No exception: the sync completed, and that is what the caller is told.
    await jobs.delta_sync_mailbox(
        None, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )


# --- retention --------------------------------------------------------------


async def test_a_mailbox_keeps_only_its_most_recent_events(agencies) -> None:
    """Bounded by the writer rather than by a purge on a timer: a mailbox
    syncing every few minutes writes faster than any schedule could chase, so a
    timer would decide how far the table overshoots, not whether it does."""
    tenant_id, _user_id, mailbox_id = await agencies("agency-a")

    for n in range(settings.SYNC_ACTIVITY_KEEP_PER_MAILBOX + 4):
        await jobs.record_sync_event(
            tenant_id, mailbox_id, KIND_DELTA_SYNC, OUTCOME_SUCCEEDED, f"event {n}"
        )

    rows = await _events(tenant_id)
    assert len(rows) == settings.SYNC_ACTIVITY_KEEP_PER_MAILBOX
    # The newest survive: the oldest line is the one nobody is reading.
    assert rows[-1].detail == f"event {settings.SYNC_ACTIVITY_KEEP_PER_MAILBOX + 3}"


async def test_the_trim_never_reaches_another_mailbox(agencies) -> None:
    """The cap is per mailbox. A DELETE that counted rows across the tenant
    would let a busy mailbox silently erase a quiet one's only failure."""
    tenant_id, _user_id, mailbox_a = await agencies("agency-a")
    _tenant_b, _user_b, mailbox_b = await agencies("agency-b")

    # Same tenant, second mailbox — the case a tenant-wide trim would break.
    async with tenant_session(tenant_id) as session:
        mailbox_second = uuid.uuid4()
        user_second = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role)"
                " VALUES (:id, :t, :e, 'member')"
            ),
            {"id": user_second, "t": tenant_id, "e": f"{mailbox_second.hex[:8]}@a.sg"},
        )
        await session.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, folder_id, scope,"
                "  retention_months)"
                " VALUES (:id, :t, :u, :m, 'archive', 'folder', :r)"
            ),
            {
                "id": mailbox_second,
                "t": tenant_id,
                "u": user_second,
                "m": f"oid-{mailbox_second.hex[:8]}",
                "r": settings.DEFAULT_RETENTION_MONTHS,
            },
        )

    await jobs.record_sync_event(
        tenant_id, mailbox_second, KIND_DELTA_SYNC, OUTCOME_FAILED, "the quiet one"
    )
    for n in range(settings.SYNC_ACTIVITY_KEEP_PER_MAILBOX + 4):
        await jobs.record_sync_event(
            tenant_id, mailbox_a, KIND_DELTA_SYNC, OUTCOME_SUCCEEDED, f"busy {n}"
        )

    details = [row.detail for row in await _events(tenant_id)]
    assert "the quiet one" in details


async def test_the_endpoint_is_newest_first_and_bounded(
    monkeypatch, client, agencies
) -> None:
    """Newest first, because the only question the panel answers is "did the
    last sync work". The cap is configuration, not a literal in the endpoint."""
    tenant_id, user_id, mailbox_id = await agencies("agency-a")
    monkeypatch.setattr(settings, "SYNC_ACTIVITY_KEEP_PER_MAILBOX", 10)
    monkeypatch.setattr(settings, "SYNC_ACTIVITY_PAGE_LIMIT", 2)
    for n in range(4):
        await jobs.record_sync_event(
            tenant_id, mailbox_id, KIND_DELTA_SYNC, OUTCOME_SUCCEEDED, f"event {n}"
        )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/mailbox/activity")).json()

    assert body["limit"] == 2
    assert [event["detail"] for event in body["events"]] == ["event 3", "event 2"]
    # ISO, so the browser can render it in the recruiter's own timezone.
    assert body["events"][0]["at"].startswith("20")
