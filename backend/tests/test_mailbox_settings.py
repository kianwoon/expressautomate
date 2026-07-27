"""Changing how far back the mailbox is read (plan §6.2).

The setting is one-directional and the UI says so, but the UI is not what
enforces it. Three things this file holds onto:

- Extending actually re-runs the walk. Moving `initial_sync_from` earlier
  without clearing `backfill_completed_at` enqueues a job that returns at its
  second `if` and logs success — the setting would change and nothing would be
  imported.
- A same-or-later window is refused with a reason. Accepting it would rewrite
  the stored date while removing no email at all, telling the user their
  history had shrunk when it had not.
- Agency A cannot reach Agency B's mailbox. Every read and write goes through
  `tenant_session`, and this is the test that proves the endpoint does too.

allow-hardcode: the SQL below is a test fixture, not a tunable.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.api import mailbox as mailbox_api
from app.api.auth import SESSION_COOKIE, _session_serializer
from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models import User
from tests.conftest import AdminSessionLocal

SETTINGS = "/api/mailbox/settings"
LOOKBACK = "/api/mailbox/settings/lookback"


@pytest.fixture(autouse=True)
def settings_the_suite_supplies(monkeypatch) -> None:
    """CI has no `.env`, so the suite states every value it depends on.

    Stated unconditionally rather than as a fallback: reading the cap off a
    developer's `.env` would test different windows locally than in CI, and
    these assertions are about which windows are offered.
    """
    monkeypatch.setattr(settings, "INITIAL_SYNC_MAX_LOOKBACK_DAYS", 90)
    monkeypatch.setattr(settings, "LOOKBACK_EXTENSION_MIN_DAYS", 7)


@pytest.fixture
def queued(monkeypatch) -> list[tuple[str, dict]]:
    """The queue, captured. Redis is not part of what this endpoint promises."""
    seen: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        seen.append((name, kwargs))
        return True

    monkeypatch.setattr(mailbox_api, "enqueue", _enqueue)
    return seen


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

    async def make(slug: str, *, days_back: int | None = 30, backfilled: bool = True):
        tenant_id, user_id, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        since = None if days_back is None else datetime.now(UTC) - timedelta(days=days_back)
        async with AdminSessionLocal() as s:
            await s.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
                {"i": tenant_id, "n": slug, "s": f"{slug}-{tenant_id.hex[:8]}"},
            )
            # The ORM for the user: `users.role` is NOT NULL with a Python-side
            # default that a hand-written INSERT never fires.
            s.add(User(id=user_id, tenant_id=tenant_id, email=f"{tenant_id.hex[:8]}@{slug}.sg"))
            # Raw SQL does not autoflush, so without this the FK below sees a
            # user that has not been written yet.
            await s.flush()
            await s.execute(
                text(
                    "INSERT INTO mailboxes"
                    " (id, tenant_id, user_id, ms_user_id, scope, folder_id,"
                    "  initial_sync_from, backfill_completed_at, retention_months)"
                    " VALUES (:i, :t, :u, :m, 'whole_inbox', 'inbox', :since, :done, :r)"
                ),
                {
                    "i": mailbox_id,
                    "t": tenant_id,
                    "u": user_id,
                    "m": f"oid-{tenant_id.hex[:8]}",
                    "since": since,
                    "done": datetime.now(UTC) if backfilled else None,
                    "r": settings.DEFAULT_RETENTION_MONTHS,
                },
            )
            await s.commit()
        tenants.append(tenant_id)
        return tenant_id, user_id, mailbox_id

    yield make

    for tid in tenants:
        async with tenant_session(tid) as s:
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
                    "SELECT initial_sync_from, backfill_completed_at"
                    " FROM mailboxes WHERE id = :i"
                ),
                {"i": mailbox_id},
            )
        ).one()


async def test_extending_moves_the_date_back_and_re_runs_the_backfill(
    client, seeded, queued
) -> None:
    """The behaviour the whole feature is for.

    Both halves are asserted because either alone is a silent failure: a date
    with no job imports nothing, and a job with a stale
    `backfill_completed_at` returns before it reads a single message.
    """
    tenant_id, user_id, mailbox_id = await seeded("agency-a", days_back=30)
    before = await _mailbox_row(tenant_id, mailbox_id)

    sign_in(client, user_id, tenant_id)
    res = await client.post(LOOKBACK, json={"window": "90d"})
    assert res.status_code == 200

    after = await _mailbox_row(tenant_id, mailbox_id)
    assert after.initial_sync_from < before.initial_sync_from
    assert after.backfill_completed_at is None
    assert [name for name, _ in queued] == ["backfill_mailbox_job"]
    assert queued[0][1] == {"tenant_id": str(tenant_id), "mailbox_id": str(mailbox_id)}


async def test_a_same_or_later_period_is_refused_rather_than_silently_ignored(
    client, seeded, queued
) -> None:
    """Moving the date later un-imports nothing, so accepting it would lie.

    "From now on" is the sharpest version: it reads as "forget my history" and
    would in fact keep every email while claiming the opposite.
    """
    tenant_id, user_id, mailbox_id = await seeded("agency-a", days_back=30)
    before = await _mailbox_row(tenant_id, mailbox_id)
    sign_in(client, user_id, tenant_id)

    for window in ("now", "7d", "30d"):
        res = await client.post(LOOKBACK, json={"window": window})
        assert res.status_code == 400, window
        assert "further back" in res.json()["detail"]

    after = await _mailbox_row(tenant_id, mailbox_id)
    assert after.initial_sync_from == before.initial_sync_from
    assert after.backfill_completed_at is not None
    assert queued == []


async def test_only_periods_further_back_than_the_current_one_are_offered(
    client, seeded
) -> None:
    """The UI must not be the only thing that knows the setting is one-way."""
    tenant_id, user_id, _mailbox_id = await seeded("agency-a", days_back=30)
    sign_in(client, user_id, tenant_id)

    body = (await client.get(SETTINGS)).json()

    assert [o["key"] for o in body["options"]] == ["90d"]
    assert body["backfill_complete"] is True
    # The current setting is returned so the page can say what is being changed
    # *from* — a period picker with no stated starting point is a guess.
    assert body["initial_sync_from"].startswith(str(datetime.now(UTC).year))


async def test_the_longest_period_leaves_nothing_to_extend_to(client, seeded) -> None:
    """Already at the cap is a real state, and an empty list is the honest
    answer to it — better than offering a re-walk that gains a day."""
    tenant_id, user_id, _mailbox_id = await seeded("agency-a", days_back=90)
    sign_in(client, user_id, tenant_id)

    assert (await client.get(SETTINGS)).json()["options"] == []


async def test_a_mailbox_that_has_never_chosen_a_period_has_nothing_to_change(
    client, seeded
) -> None:
    """Onboarding owns the first choice; this screen must not become a second
    place it can be made."""
    tenant_id, user_id, _mailbox_id = await seeded("agency-a", days_back=None)
    sign_in(client, user_id, tenant_id)

    assert (await client.get(SETTINGS)).status_code == 404
    assert (await client.post(LOOKBACK, json={"window": "90d"})).status_code == 404


async def test_one_agency_cannot_extend_another_agencys_mailbox(
    client, seeded, queued
) -> None:
    """The security property. A cross-tenant write here would re-walk a
    competitor's inbox on their Graph quota and against their consent."""
    tenant_a, user_a, _mailbox_a = await seeded("agency-a", days_back=None)
    tenant_b, _user_b, mailbox_b = await seeded("agency-b", days_back=30)

    # Agency A's session, and A has no period set. If the endpoint leaked
    # across tenants it would find B's row and happily rewrite it.
    sign_in(client, user_a, tenant_a)
    assert (await client.post(LOOKBACK, json={"window": "90d"})).status_code == 404

    untouched = await _mailbox_row(tenant_b, mailbox_b)
    assert untouched.backfill_completed_at is not None
    assert queued == []


async def test_signed_out_callers_are_refused(client) -> None:
    """No session, no setting — and no hint about whether a mailbox exists."""
    assert (await client.get(SETTINGS)).status_code == 401
    assert (await client.post(LOOKBACK, json={"window": "90d"})).status_code == 401
