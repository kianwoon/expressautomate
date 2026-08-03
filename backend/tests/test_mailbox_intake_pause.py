"""The mailbox intake pause (spec: 2026-08-03-mailbox-intake-pause-design.md).

Mail enters by three doors — the webhook, the scheduled delta sweep, and the
lifecycle-triggered jobs — and the tests here follow that shape. The webhook
one matters most: it is the *primary* intake path and it never consults
`active_mailboxes()`, so a pause tested only against the sweep passes its
suite and ingests every vacation email in production.

The other trap is resume. Clearing `delta_link` does not mean "start from
now" — a null checkpoint makes the next walk replay the entire folder — so
the pinning test at the bottom holds the fresh-cursor behaviour in place.

allow-hardcode: the SQL and Graph payloads below are test fixtures.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.api import graph_webhook
from app.api import mailbox as mailbox_api
from app.api.auth import SESSION_COOKIE, _session_serializer
from app.core.config import settings
from app.db.rls import tenant_session
from app.db.session import SessionLocal
from app.main import app
from app.models import User
from app.services import ms_auth
from app.services.graph import delta as delta_module
from app.services.graph.client import GraphClient
from app.workers import jobs, tasks
from tests.conftest import AdminSessionLocal

PAUSE = "/api/mailbox/pause"
RESUME = "/api/mailbox/resume"
SETTINGS = "/api/mailbox/settings"

OLD_LINK = "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=old"
NEW_LINK = "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=fresh"


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """ASGI transport, not TestClient: TestClient drives its own event loop and
    the engine in app.db.session is pinned to the session-scoped one."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        yield c


@pytest.fixture
async def seeded():
    """A factory for an agency with one inbox already being read.

    Seeded through the admin role because RLS is among the things under test:
    fixtures written through the restricted role would "prove" isolation by
    never having inserted the other tenant's rows at all.
    """
    tenants: list[uuid.UUID] = []

    async def make(
        slug: str,
        *,
        paused: bool = False,
        delta_link: str | None = None,
        tenant_id: uuid.UUID | None = None,
    ):
        new_tenant = tenant_id is None
        tenant_id = tenant_id or uuid.uuid4()
        user_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
        async with AdminSessionLocal() as s:
            if new_tenant:
                await s.execute(
                    text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
                    {"i": tenant_id, "n": slug, "s": f"{slug}-{tenant_id.hex[:8]}"},
                )
            # The ORM for the user: `users.role` is NOT NULL with a Python-side
            # default that a hand-written INSERT never fires.
            s.add(User(id=user_id, tenant_id=tenant_id, email=f"{user_id.hex[:8]}@{slug}.sg"))
            await s.flush()
            await s.execute(
                text(
                    "INSERT INTO mailboxes"
                    " (id, tenant_id, user_id, ms_user_id, scope, folder_id,"
                    "  initial_sync_from, backfill_completed_at, delta_link,"
                    "  ingest_paused_at, retention_months)"
                    " VALUES (:i, :t, :u, :m, 'whole_inbox', 'inbox', :since,"
                    "         now(), :link, :paused, :r)"
                ),
                {
                    "i": mailbox_id,
                    "t": tenant_id,
                    "u": user_id,
                    "m": f"oid-{user_id.hex[:8]}",
                    "since": datetime.now(UTC) - timedelta(days=30),
                    "link": delta_link,
                    "paused": datetime.now(UTC) if paused else None,
                    "r": settings.DEFAULT_RETENTION_MONTHS,
                },
            )
            await s.commit()
        if new_tenant:
            tenants.append(tenant_id)
        return tenant_id, user_id, mailbox_id

    yield make

    for tid in tenants:
        async with tenant_session(tid) as s:
            await s.execute(text("DELETE FROM graph_subscriptions"))
            await s.execute(text("DELETE FROM email_messages"))
            await s.execute(text("DELETE FROM mailboxes"))
            await s.execute(text("DELETE FROM users"))
            await s.execute(text("DELETE FROM tenants"))


def sign_in(client: httpx.AsyncClient, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """The cookie the OAuth callback would have set, without the OAuth."""
    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )


async def _mailbox_row(tenant_id: uuid.UUID, mailbox_id: uuid.UUID):
    async with tenant_session(tenant_id) as s:
        return (
            await s.execute(
                text(
                    "SELECT ingest_paused_at, delta_link, backfill_completed_at"
                    " FROM mailboxes WHERE id = :i"
                ),
                {"i": mailbox_id},
            )
        ).one()


async def _email_rows(tenant_id: uuid.UUID) -> list:
    async with tenant_session(tenant_id) as s:
        return (
            await s.execute(
                text("SELECT graph_message_id, processing_status FROM email_messages")
            )
        ).all()


async def _subscribe(tenant_id: uuid.UUID, mailbox_id: uuid.UUID, sub: str, state: str):
    """An active Graph subscription, so the webhook resolves this mailbox."""
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO graph_subscriptions"
                " (id, tenant_id, mailbox_id, subscription_id, resource,"
                "  client_state, expires_at)"
                " VALUES (:i, :t, :m, :sub, 'r', :state, now() + interval '1 minute')"
            ),
            {
                "i": uuid.uuid4(),
                "t": tenant_id,
                "m": mailbox_id,
                "sub": sub,
                "state": state,
            },
        )
        await s.commit()


# --- the resolver ------------------------------------------------------------


async def test_the_resolver_excludes_paused_and_keeps_active_mailboxes(seeded) -> None:
    tenant_id, _, running_id = await seeded("agency-a")
    _, _, paused_id = await seeded("agency-a", paused=True, tenant_id=tenant_id)

    # Through the unscoped runtime session, exactly as `delta_sync_all` calls
    # it — a SECURITY DEFINER function proven through the admin role proves
    # nothing about what the app role can reach.
    async with SessionLocal() as s:
        rows = (await s.execute(text("SELECT * FROM active_mailboxes()"))).all()

    listed = {row.mailbox_id for row in rows}
    assert running_id in listed
    assert paused_id not in listed


async def test_the_resolver_is_still_security_definer(admin_session) -> None:
    """`CREATE OR REPLACE` without SECURITY DEFINER silently reverts the
    function to SECURITY INVOKER — under FORCE ROW LEVEL SECURITY the sweep
    would then see one tenant's mailboxes and quietly stop for everyone else.
    The search_path pin travels with it, for the same silent-loss reason."""
    row = (
        await admin_session.execute(
            text(
                "SELECT prosecdef, proconfig FROM pg_proc"
                " WHERE proname = 'active_mailboxes'"
            )
        )
    ).one()

    assert row.prosecdef is True
    assert any("search_path" in entry for entry in (row.proconfig or []))


async def test_renewal_still_covers_a_paused_mailbox(seeded) -> None:
    """The subscription must stay current through the pause, or it lapses
    mid-vacation and resume has to recreate it — the very failure a
    `status = 'paused'` enum value would have caused."""
    tenant_id, _, mailbox_id = await seeded("agency-a", paused=True)
    await _subscribe(tenant_id, mailbox_id, f"sub-{mailbox_id.hex[:8]}", "secret")

    # Margin 0.0 makes every active subscription due immediately, which keeps
    # the test about the join rather than about clock arithmetic.
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                text("SELECT * FROM subscriptions_due_for_renewal(0.0)")
            )
        ).all()

    assert mailbox_id in {row.mailbox_id for row in rows}


# --- the sweep ----------------------------------------------------------------


async def test_the_sweep_fans_out_to_no_paused_mailbox(seeded, monkeypatch) -> None:
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)

    tenant_id, _, running_id = await seeded("agency-a")
    await seeded("agency-a", paused=True, tenant_id=tenant_id)

    await tasks.delta_sync_all()

    swept = {kwargs["mailbox_id"] for _, kwargs in queued}
    assert str(running_id) in swept
    assert len(swept) == 1


# --- the webhook: the door the sweep never guards ------------------------------


async def test_a_webhook_notification_for_a_paused_mailbox_ingests_nothing(
    seeded, client, monkeypatch
) -> None:
    """The test that catches the real intake path. The subscription stays
    alive during a pause by design, so Graph keeps notifying — and without
    the early drop every vacation email arrives in real time through a gate
    `active_mailboxes()` never guarded."""
    enqueued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        enqueued.append((name, kwargs))
        return True

    monkeypatch.setattr(graph_webhook, "enqueue", _enqueue)

    tenant_id, _, mailbox_id = await seeded("agency-a", paused=True)
    sub, state = f"sub-{mailbox_id.hex[:8]}", "secret-state"
    await _subscribe(tenant_id, mailbox_id, sub, state)

    response = await client.post(
        "/api/graph/notifications",
        json={
            "value": [
                {
                    "subscriptionId": sub,
                    "clientState": state,
                    "resourceData": {"id": "MSG-VACATION"},
                }
            ]
        },
    )

    # Still 2xx — Graph retries anything else for hours, and a retried
    # notification for a paused mailbox would never start working.
    assert response.status_code == 202
    assert await _email_rows(tenant_id) == []
    assert enqueued == []


# --- the job gates: where every door funnels through ---------------------------


async def test_fetch_email_returns_early_when_the_mailbox_is_paused(
    seeded, monkeypatch
) -> None:
    """The gate is at the job, not the caller: whatever enqueued this — a
    webhook race, a sweep, `rescan_stuck` — the fetch itself must refuse."""
    tenant_id, _, mailbox_id = await seeded("agency-a", paused=True)
    row_id = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        await s.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, processing_status)"
                " VALUES (:i, :t, :m, 'MSG-1', 'pending')"
            ),
            {"i": row_id, "t": tenant_id, "m": mailbox_id},
        )

    async def _no_graph(*args, **kwargs):
        raise AssertionError("a paused mailbox must not cost a token refresh")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _no_graph)

    await jobs.fetch_email(
        None,
        email_message_id=str(row_id),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )

    rows = await _email_rows(tenant_id)
    # Left `pending`, not consumed: a row that reaches this gate was recorded
    # before the pause, and `rescan_stuck` picks it up once intake resumes.
    assert rows == [("MSG-1", "pending")]


async def test_delta_sync_mailbox_returns_early_when_the_mailbox_is_paused(
    seeded, monkeypatch
) -> None:
    """Enqueued directly by a `missed` lifecycle event and by
    `recreate_subscription`, so the sweep's own filter is not enough."""
    tenant_id, _, mailbox_id = await seeded("agency-a", paused=True, delta_link=OLD_LINK)

    async def _no_graph(*args, **kwargs):
        raise AssertionError("a paused mailbox must not be walked")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _no_graph)

    await jobs.delta_sync_mailbox(
        None, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    row = await _mailbox_row(tenant_id, mailbox_id)
    assert row.delta_link == OLD_LINK, "the checkpoint must not move during a pause"


# --- the routes ----------------------------------------------------------------


async def test_pausing_is_idempotent_and_the_settings_payload_says_so(
    seeded, client
) -> None:
    tenant_id, user_id, mailbox_id = await seeded("agency-a")
    sign_in(client, user_id, tenant_id)

    first = await client.post(PAUSE)
    assert first.status_code == 200
    paused_at = first.json()["paused_at"]
    assert paused_at is not None

    # A double-click must not move the "paused since" date the UI shows.
    second = await client.post(PAUSE)
    assert second.status_code == 200
    assert second.json()["paused_at"] == paused_at

    shown = await client.get(SETTINGS)
    assert shown.status_code == 200
    assert shown.json()["ingest_paused_at"] == paused_at


async def test_resume_stores_a_fresh_cursor_and_leaves_the_backfill_alone(
    seeded, client, monkeypatch
) -> None:
    """Resume means "from now": a filtered walk mints a new deltaLink with the
    paused window already behind it. `backfill_completed_at` stays set —
    clearing it would queue a backfill of exactly the window this feature
    exists to skip."""
    tenant_id, user_id, mailbox_id = await seeded(
        "agency-a", paused=True, delta_link=OLD_LINK
    )
    sign_in(client, user_id, tenant_id)

    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(mailbox_api, "enqueue", _enqueue)
    monkeypatch.setattr(delta_module, "enqueue", _enqueue)

    async def _token(tenant, user):
        return "t"

    monkeypatch.setattr(ms_auth, "access_token_for_user", _token)

    walked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        walked.append(str(request.url))
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": NEW_LINK})

    monkeypatch.setattr(
        mailbox_api,
        "GraphClient",
        lambda token: GraphClient(token, transport=httpx.MockTransport(handler)),
    )

    response = await client.post(RESUME)

    assert response.status_code == 200
    assert response.json()["resumed_from"] is not None

    row = await _mailbox_row(tenant_id, mailbox_id)
    assert row.ingest_paused_at is None
    assert row.delta_link == NEW_LINK
    assert row.backfill_completed_at is not None
    assert [n for n, _ in queued] == [], "no backfill, no catch-up"

    # The walk that minted the cursor started from `now`, not from the stale
    # checkpoint and not from the bare folder URL — either of those replays.
    assert len(walked) == 1
    assert "$filter=receivedDateTime" in walked[0]
    assert "deltatoken=old" not in walked[0]


async def test_resuming_a_running_mailbox_keeps_the_cursor(
    seeded, client, monkeypatch
) -> None:
    tenant_id, user_id, mailbox_id = await seeded("agency-a", delta_link=OLD_LINK)
    sign_in(client, user_id, tenant_id)

    def _no_graph(token):
        raise AssertionError("an idle resume must not reach Graph")

    monkeypatch.setattr(mailbox_api, "GraphClient", _no_graph)

    response = await client.post(RESUME)

    assert response.status_code == 200
    assert response.json()["resumed_from"] is None
    row = await _mailbox_row(tenant_id, mailbox_id)
    assert row.delta_link == OLD_LINK, "a double-click must not discard the checkpoint"


async def test_a_failed_walk_leaves_the_mailbox_paused(
    seeded, client, monkeypatch
) -> None:
    """The order is load-bearing: unpausing before the fresh cursor is stored
    would let the scheduled sweep resume from the pre-pause checkpoint. So a
    resume whose walk fails must change nothing and say so."""
    tenant_id, user_id, mailbox_id = await seeded(
        "agency-a", paused=True, delta_link=OLD_LINK
    )
    sign_in(client, user_id, tenant_id)

    async def _token(tenant, user):
        return "t"

    monkeypatch.setattr(ms_auth, "access_token_for_user", _token)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    monkeypatch.setattr(
        mailbox_api,
        "GraphClient",
        lambda token: GraphClient(token, transport=httpx.MockTransport(handler)),
    )

    response = await client.post(RESUME)

    assert response.status_code == 502
    assert "still paused" in response.json()["detail"]
    row = await _mailbox_row(tenant_id, mailbox_id)
    assert row.ingest_paused_at is not None
    assert row.delta_link == OLD_LINK


async def test_a_user_pauses_their_own_mailbox_and_nobody_elses(
    seeded, client
) -> None:
    """No mailbox id in the path, so the only mailbox reachable is the
    caller's own — this holds that the resolution really is by session."""
    tenant_id, owner_id, owner_mailbox = await seeded("agency-a")
    _, colleague_id, colleague_mailbox = await seeded(
        "agency-a", tenant_id=tenant_id
    )

    sign_in(client, colleague_id, tenant_id)
    response = await client.post(PAUSE)
    assert response.status_code == 200

    theirs = await _mailbox_row(tenant_id, colleague_mailbox)
    ours = await _mailbox_row(tenant_id, owner_mailbox)
    assert theirs.ingest_paused_at is not None
    assert ours.ingest_paused_at is None, "a colleague's pause must not touch this mailbox"


async def test_the_routes_refuse_the_signed_out_and_the_unprovisioned(client, seeded) -> None:
    for path in (PAUSE, RESUME):
        assert (await client.post(path)).status_code == 401

    tenant_id, user_id, _ = await seeded("agency-a")
    async with tenant_session(tenant_id) as s:
        await s.execute(text("DELETE FROM mailboxes"))
    sign_in(client, user_id, tenant_id)
    for path in (PAUSE, RESUME):
        assert (await client.post(path)).status_code == 404


# --- the pinning test -----------------------------------------------------------


async def test_mail_arriving_during_a_pause_is_never_ingested(
    seeded, client, monkeypatch
) -> None:
    """Pause, let mail arrive, resume: none of it may exist afterwards.

    This is the one that catches an unbounded replay if the fresh-cursor
    logic regresses — a resume that clears `delta_link` instead of replacing
    it walks the whole folder, vacation included, on the next sweep.
    """
    tenant_id, user_id, mailbox_id = await seeded(
        "agency-a", paused=True, delta_link=OLD_LINK
    )
    sub, state = f"sub-{mailbox_id.hex[:8]}", "secret-state"
    await _subscribe(tenant_id, mailbox_id, sub, state)

    enqueued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        enqueued.append((name, kwargs))
        return True

    monkeypatch.setattr(graph_webhook, "enqueue", _enqueue)
    monkeypatch.setattr(delta_module, "enqueue", _enqueue)

    # Mail arrives during the pause, by the primary door.
    await client.post(
        "/api/graph/notifications",
        json={
            "value": [
                {
                    "subscriptionId": sub,
                    "clientState": state,
                    "resourceData": {"id": "MSG-WHILE-AWAY"},
                }
            ]
        },
    )

    async def _token(tenant, user):
        return "t"

    monkeypatch.setattr(ms_auth, "access_token_for_user", _token)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # The regression this test exists for: a walk from the stale
        # checkpoint or from the bare folder URL replays the paused window.
        assert "deltatoken=old" not in url, "resume walked the pre-pause checkpoint"
        assert "$filter=receivedDateTime" in url, "resume walked unbounded folder history"
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": NEW_LINK})

    monkeypatch.setattr(
        mailbox_api,
        "GraphClient",
        lambda token: GraphClient(token, transport=httpx.MockTransport(handler)),
    )

    sign_in(client, user_id, tenant_id)
    assert (await client.post(RESUME)).status_code == 200

    assert await _email_rows(tenant_id) == [], "paused-window mail was ingested"
    row = await _mailbox_row(tenant_id, mailbox_id)
    assert row.delta_link == NEW_LINK
    assert enqueued == []
