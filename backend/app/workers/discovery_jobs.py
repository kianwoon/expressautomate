"""The arq job that scans one recruiter's mailbox headers for clients.

Its own module for the reason `sourcing_jobs.py` gives about itself: `jobs.py`
sits at the repo's 1500-line ceiling, and a discovery scan shares nothing with
mail ingestion but the queue it arrives on.

**The job carries its tenant**, like every other job here — background work
has no request and therefore no session tenant, and a job naming a mismatched
(tenant, run) pair reads no row under the tenant policy and quietly does
nothing.

**Failure discipline** is simpler than sourcing's on purpose. A failure is
written onto the row in words the recruiter can act on, and the retry is
their click. The two exceptions: a Graph throttle re-queues itself via `Retry`
for the delay Graph named, and a worker killed outright leaves the row
`running` — the supervisor sweep (`sweep_stale_client_discovery_runs` in
`app/workers/tasks.py`) parks a `running` row this stale in `failed`, and the
scan endpoint supersedes a stale `running` row too, whichever happens first.
A `pending` row whose enqueue was lost after commit (the job never existed)
is likewise swept to `failed` rather than left unclaimed forever.
"""

import uuid
from datetime import UTC, datetime, timedelta

from arq import Retry
from sqlalchemy import select, text, update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.client_discovery import ClientDiscoveryRun
from app.services import client_discovery, ms_auth
from app.services.graph.client import GraphAuthError, GraphClient, GraphThrottled

log = get_logger(__name__)

# `running` is accepted deliberately: arq re-runs a job whose worker died
# mid-flight, and accepting only `pending` would strand exactly those runs.
# `done` and `failed` are answers — replaying on either changes nothing.
_RESUMABLE = (ClientDiscoveryRun.PENDING, ClientDiscoveryRun.RUNNING)

# allow-hardcode: SQL statements, not a phrase list.
_USER_EMAIL = text("SELECT email FROM users WHERE id = :user_id")

# allow-hardcode: sentences shown to a recruiter, not configuration.
_RECONNECT = (
    "Microsoft would not let us read this mailbox. "
    "Reconnect your mailbox and scan again."
)
_UNREACHABLE = "Microsoft could not be reached just now. Try scanning again in a few minutes."
_APPLY_FAILED = "The scan finished but saving its results failed. Scan again to retry."


def graph_client(token: str) -> GraphClient:
    """Indirection point, so tests can hand in a mock transport."""
    return GraphClient(token)


def _own_domains(email: str | None) -> frozenset[str]:
    """The scanning recruiter's own mail domain — colleagues are not clients.

    A plain partition rather than `domain_of`: a personal account's own domain
    is a free provider, which `domain_of` maps to None, and "no own domain"
    would then fail open for the one domain this must always exclude.
    """
    _, _, domain = (email or "").partition("@")
    domain = domain.strip().lower()
    return frozenset({domain}) if domain else frozenset()


async def run_client_discovery(ctx, *, tenant_id: str, run_id: str) -> None:
    """Scan, enrich existing clients, and store the ranked new domains."""
    tenant = uuid.UUID(tenant_id)
    record = uuid.UUID(run_id)

    async with tenant_session(tenant) as session:
        run = (
            await session.execute(
                select(ClientDiscoveryRun).where(ClientDiscoveryRun.id == record)
            )
        ).scalar_one_or_none()
        if run is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info("client_discovery_skipped_unknown_run", run_id=run_id)
            return
        if run.status not in _RESUMABLE:
            log.info(
                "client_discovery_skipped_already_answered",
                run_id=run_id,
                status=run.status,
            )
            return

        # A conditional UPDATE, not the read above followed by a write — the
        # same indivisible-claim reasoning `run_sourcing` documents.
        claimed = (
            await session.execute(
                update(ClientDiscoveryRun)
                .where(
                    ClientDiscoveryRun.id == record,
                    ClientDiscoveryRun.status.in_(_RESUMABLE),
                )
                .values(status=ClientDiscoveryRun.RUNNING, started_at=datetime.now(UTC))
                .returning(
                    ClientDiscoveryRun.user_id, ClientDiscoveryRun.lookback_days
                )
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            log.info("client_discovery_skipped_claimed_elsewhere", run_id=run_id)
            return
        user_id, lookback_days = claimed
        email = (
            await session.execute(_USER_EMAIL, {"user_id": user_id})
        ).scalar_one_or_none()
        await session.commit()

    if email is None:
        # The user was deleted after starting the scan; the CASCADE will take
        # the run with them, but this attempt may still be holding it.
        await _fail(tenant, record, _RECONNECT)
        return

    try:
        token = await ms_auth.access_token_for_user(tenant, user_id)
    except ms_auth.MailboxNotAuthorised as exc:
        log.info("client_discovery_unauthorised", run_id=run_id, error=str(exc))
        await _fail(tenant, record, _RECONNECT)
        return
    except ms_auth.TokenRefreshTransientError as exc:
        # Entra throttled or was slow — the grant is fine, so this is not the
        # "reconnect" failure above and not the unreachable failure below.
        # Defer like a Graph throttle; the claim accepts `running`, so the
        # retry resumes cleanly.
        log.info("client_discovery_refresh_transient", run_id=run_id, error=str(exc))
        raise Retry(defer=settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS) from exc

    since = datetime.now(UTC) - timedelta(days=lookback_days)
    client = graph_client(token)
    try:
        result = await client_discovery.scan_headers(
            client, since=since, own_domains=_own_domains(email)
        )
    except GraphThrottled as exc:
        # Graph named its own delay; hand the job back to arq for then. The
        # claim accepts `running`, so the retry resumes cleanly.
        log.info(
            "client_discovery_throttled", run_id=run_id, retry_after=exc.retry_after
        )
        raise Retry(defer=exc.retry_after) from exc
    except GraphAuthError as exc:
        # Refreshed fine, refused at read time — revoked in between, or an
        # admin policy. "Reconnect" is the fix, exactly as the preview says.
        log.info("client_discovery_refused", run_id=run_id, error=repr(exc))
        await _fail(tenant, record, _RECONNECT)
        return
    except Exception as exc:
        # GraphError and everything httpx raises before a response exists.
        # Written onto the row rather than re-raised: the retry is a click,
        # and an arq-failed job would leave the row `running` and mute.
        log.warning("client_discovery_scan_failed", run_id=run_id, error=repr(exc))
        await _fail(tenant, record, _UNREACHABLE)
        return
    finally:
        await client.aclose()

    now = datetime.now(UTC)
    ranked = client_discovery.ranked_entries(result.domains, now=now)

    try:
        async with tenant_session(tenant) as session:
            await client_discovery.lock_contact_application(session, tenant)

            new_entries: list[dict] = []
            clients_enriched = 0
            contacts_added = 0
            for entry in ranked:
                holder = await client_discovery.existing_client_for_domain(
                    session, tenant, entry["domain"]
                )
                if holder is None:
                    new_entries.append(entry)
                    continue
                # The automatic backfill — every domain already held by a
                # client (directly or through its merge chain) enriches that
                # client and never appears in the "new" list.
                added = await client_discovery.enrich_existing_client(
                    session, tenant, holder, entry
                )
                contacts_added += added
                if added:
                    clients_enriched += 1

            kept = new_entries[: settings.CLIENT_DISCOVERY_MAX_DOMAINS]
            await session.execute(
                update(ClientDiscoveryRun)
                .where(ClientDiscoveryRun.id == record)
                .values(
                    status=ClientDiscoveryRun.DONE,
                    finished_at=datetime.now(UTC),
                    inbox_scanned=result.inbox_scanned,
                    sent_scanned=result.sent_scanned,
                    messages_truncated=result.truncated,
                    domains_truncated=len(new_entries) > len(kept),
                    clients_enriched=clients_enriched,
                    contacts_added=contacts_added,
                    results=kept,
                    error=None,
                )
                .execution_options(synchronize_session=False)
            )
    except Exception:
        log.exception("client_discovery_apply_failed", run_id=run_id)
        await _fail(tenant, record, _APPLY_FAILED)
        return

    log.info(
        "client_discovery_completed",
        run_id=run_id,
        inbox_scanned=result.inbox_scanned,
        sent_scanned=result.sent_scanned,
        new_domains=len(kept),
        clients_enriched=clients_enriched,
        contacts_added=contacts_added,
    )


async def _fail(tenant: uuid.UUID, record: uuid.UUID, error: str) -> None:
    """Park the run in `failed` with a sentence the recruiter can act on."""
    async with tenant_session(tenant) as session:
        run = await session.get(ClientDiscoveryRun, record)
        if run is None:  # pragma: no cover - deleted mid-run
            return
        run.status = ClientDiscoveryRun.FAILED
        run.finished_at = datetime.now(UTC)
        run.error = error
        await session.commit()
