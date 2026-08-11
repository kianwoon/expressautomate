"""The client-discovery stale-run sweep (spec 2026-08-02, amended 2026-08-12):
a `pending` run whose enqueue was lost (the row committed, the job never did)
and a `running` run whose worker died (SIGKILL, OOM, eviction) before any
exception handler could run both have no other owner — the sweep parks them in
`failed` with words the recruiter can act on. A run still inside its window
must be left alone: it may merely be slow, or a worker may just have claimed
it.

allow-hardcode: the SQL below is test fixture data, the same idiom
`test_wa_sweep.py` uses — written out rather than built through the ORM so a
schema change breaks these tests loudly. It is fixture setup, not a phrase
list matched against anything.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.models.client_discovery import ClientDiscoveryRun
from app.workers import tasks


async def _insert_run(
    admin_session, tenant_id, user_id, status, age_minutes
) -> uuid.UUID:
    row_id = uuid.uuid4()
    ts = datetime.now(UTC) - timedelta(minutes=age_minutes)
    await admin_session.execute(
        text(
            "INSERT INTO client_discovery_runs "
            "(id, tenant_id, user_id, status, lookback_days,"
            " inbox_scanned, sent_scanned, messages_truncated, domains_truncated,"
            " clients_enriched, contacts_added, created_at, updated_at) "
            "VALUES (:id, :tid, :uid, :status, 90,"
            " 0, 0, false, false, 0, 0, :ts, :ts)"
        ),
        {
            "id": row_id,
            "tid": tenant_id,
            "uid": user_id,
            "status": status,
            "ts": ts,
        },
    )
    await admin_session.commit()
    return row_id


async def _row(admin_session, row_id) -> tuple[str, str | None]:
    row = (
        await admin_session.execute(
            text("SELECT status, error FROM client_discovery_runs WHERE id = :id"),
            {"id": row_id},
        )
    ).one()
    return row.status, row.error


async def _seed_agency(admin_session) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    return tenant_id, user_id


async def test_stale_pending_and_running_runs_become_failed(
    admin_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_STALE_PENDING_MINUTES", 10)
    tenant_id, user_id = await _seed_agency(admin_session)

    stale_pending = await _insert_run(admin_session, tenant_id, user_id, "pending", 30)
    stale_running = await _insert_run(admin_session, tenant_id, user_id, "running", 30)
    fresh_pending = await _insert_run(admin_session, tenant_id, user_id, "pending", 1)

    swept = await tasks.sweep_stale_client_discovery_runs()
    assert swept == 2

    status, error = await _row(admin_session, stale_pending)
    assert status == ClientDiscoveryRun.FAILED
    assert error and "queued" in error

    status, error = await _row(admin_session, stale_running)
    assert status == ClientDiscoveryRun.FAILED
    assert error and "retry" in error

    # A run merely slow, not dead, must be left alone.
    status, _ = await _row(admin_session, fresh_pending)
    assert status == "pending"

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


async def test_the_sweep_never_touches_a_run_already_settled(
    admin_session, monkeypatch
) -> None:
    """A `done` or `failed` row is settled; the sweep's status compare-and-set
    must never revisit it, no matter how old."""
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_STALE_PENDING_MINUTES", 10)
    tenant_id, user_id = await _seed_agency(admin_session)

    done = await _insert_run(admin_session, tenant_id, user_id, "done", 999)
    failed = await _insert_run(admin_session, tenant_id, user_id, "failed", 999)

    swept = await tasks.sweep_stale_client_discovery_runs()
    assert swept == 0

    status, _ = await _row(admin_session, done)
    assert status == "done"
    status, _ = await _row(admin_session, failed)
    assert status == "failed"

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


async def test_a_recently_claimed_running_run_is_left_alone(
    admin_session, monkeypatch
) -> None:
    """The `running` branch gates on `updated_at` — the claim's write — so a
    worker that just claimed the row (and is still very much alive) is never
    swept out from under it, even if the row's `created_at` is old."""
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_STALE_PENDING_MINUTES", 10)
    tenant_id, user_id = await _seed_agency(admin_session)

    run_id = uuid.uuid4()
    old = datetime.now(UTC) - timedelta(minutes=30)
    now = datetime.now(UTC)
    await admin_session.execute(
        text(
            "INSERT INTO client_discovery_runs "
            "(id, tenant_id, user_id, status, lookback_days,"
            " inbox_scanned, sent_scanned, messages_truncated, domains_truncated,"
            " clients_enriched, contacts_added, created_at, updated_at) "
            "VALUES (:id, :tid, :uid, 'running', 90,"
            " 0, 0, false, false, 0, 0, :old, :now)"
        ),
        {"id": run_id, "tid": tenant_id, "uid": user_id, "old": old, "now": now},
    )
    await admin_session.commit()

    swept = await tasks.sweep_stale_client_discovery_runs()
    assert swept == 0

    status, _ = await _row(admin_session, run_id)
    assert status == "running"

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()
