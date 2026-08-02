"""Client auto-discovery: start a scan, poll it, create what it found.

Settings-page routes (spec 2026-08-02). Three endpoints, in the order a
recruiter meets them: `POST /client-discovery/scan` queues a header-only walk
of their own mailbox, `GET /client-discovery` reads the latest run back, and
`POST /client-discovery/clients` turns selected new domains into `confirmed`
clients with their contacts.

Only the scan POST needs the mailbox grant — it goes through the same
`_connected_user` gate as the inbox preview, so "connect your mailbox first"
and "this account has no Microsoft mailbox" are answered here in words rather
than surfacing as an opaque worker failure. The GET and the create POST read
and write only our own rows and deliberately need just a session, for the
reason `mailbox_settings` gives: a lapsed grant must not lock someone out of
results that already exist.

There is deliberately no `graph_configured()` check here: the scan's Graph
calls happen on the workers, which have their own env — gating on this
process's env would block a path that works (the same reasoning
`/mailbox/ingest` records).
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.auth import _require_session
from app.api.mailbox import _connected_user
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.client_discovery import ClientDiscoveryRun
from app.services.client_discovery import (
    create_client_from_entry,
    lock_contact_application,
)
from app.workers.queue import enqueue

log = get_logger(__name__)
router = APIRouter(tags=["client-discovery"])

_ACTIVE = (ClientDiscoveryRun.PENDING, ClientDiscoveryRun.RUNNING)

# allow-hardcode: a sentence shown to a recruiter, not configuration.
_ENQUEUE_FAILED = (
    "The scan could not be queued just now. This is usually momentary — "
    "scan again in a minute."
)


class CreateClientsRequest(BaseModel):
    """Which of the run's new domains to create as clients."""

    domains: list[str] = Field(min_length=1)


def serialize_run(run: ClientDiscoveryRun) -> dict:
    return {
        "id": str(run.id),
        "status": run.status,
        "lookback_days": run.lookback_days,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "inbox_scanned": run.inbox_scanned,
        "sent_scanned": run.sent_scanned,
        "messages_truncated": run.messages_truncated,
        "domains_truncated": run.domains_truncated,
        "clients_enriched": run.clients_enriched,
        "contacts_added": run.contacts_added,
        "error": run.error,
        # Results exist only on a finished run; anything else answers null so
        # a polling page never renders a half-written list.
        "results": run.results if run.status == ClientDiscoveryRun.DONE else None,
    }


async def _latest_run(session, user_id: uuid.UUID) -> ClientDiscoveryRun | None:
    """This user's most recent run. `id` breaks a same-instant tie."""
    return (
        await session.execute(
            select(ClientDiscoveryRun)
            .where(ClientDiscoveryRun.user_id == user_id)
            .order_by(
                ClientDiscoveryRun.created_at.desc(), ClientDiscoveryRun.id.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post("/client-discovery/scan", status_code=202)
async def start_scan(request: Request) -> dict:
    """Queue a header-only scan of the signed-in user's mailbox.

    202, not 201: the row exists but the answer does not — a mailbox's worth
    of header pages has no business inside a request. One live scan per user:
    a fresh `pending`/`running` run is a 409, a stale one (dead worker) is
    superseded, and earlier finished runs are deleted so the table stays one
    working set per user rather than a history.
    """
    tenant_uuid, user_id, _ms_user_id = await _connected_user(request)

    async with tenant_session(tenant_uuid) as session:
        latest = await _latest_run(session, user_id)
        if latest is not None and latest.status in _ACTIVE:
            stale_after = (latest.created_at or datetime.now(UTC)) + timedelta(
                minutes=settings.CLIENT_DISCOVERY_STALE_RUNNING_MINUTES
            )
            if datetime.now(UTC) < stale_after:
                raise HTTPException(
                    status_code=409,
                    detail="A scan is already running. Reload in a moment to see it.",
                )
        await session.execute(
            delete(ClientDiscoveryRun).where(ClientDiscoveryRun.user_id == user_id)
        )
        run_id = uuid.uuid4()
        session.add(
            ClientDiscoveryRun(
                id=run_id,
                tenant_id=tenant_uuid,
                user_id=user_id,
                status=ClientDiscoveryRun.PENDING,
                lookback_days=settings.CLIENT_DISCOVERY_LOOKBACK_DAYS,
            )
        )

    # Enqueued after the commit, because the job reads the row it is named
    # for. `enqueue` never raises; a refusal here would otherwise leave a
    # `pending` row nothing will ever claim — nothing sweeps discovery runs.
    if not await enqueue(
        "run_client_discovery", tenant_id=str(tenant_uuid), run_id=str(run_id)
    ):
        log.warning("client_discovery_enqueue_failed", run_id=str(run_id))
        async with tenant_session(tenant_uuid) as session:
            record = await session.get(ClientDiscoveryRun, run_id)
            record.status = ClientDiscoveryRun.FAILED
            record.finished_at = datetime.now(UTC)
            record.error = _ENQUEUE_FAILED
            return serialize_run(record)

    log.info(
        "client_discovery_scan_started",
        tenant_id=str(tenant_uuid),
        run_id=str(run_id),
    )
    async with tenant_session(tenant_uuid) as session:
        return serialize_run(await session.get(ClientDiscoveryRun, run_id))


@router.get("/client-discovery")
async def latest_discovery(request: Request) -> dict:
    """The signed-in user's most recent run, results included when done.

    "No run yet" answers 200 with a null run rather than 404 — it is a state
    of a page that exists, not a missing resource.
    """
    user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        run = await _latest_run(session, user_uuid)
        return {"run": serialize_run(run) if run is not None else None}


@router.post("/client-discovery/clients")
async def create_clients(request: Request, body: CreateClientsRequest) -> dict:
    """Create the selected new domains as confirmed clients, with contacts.

    Answers per domain rather than all-or-nothing: `created`,
    `already_existed` (something else created the client between scan and
    click — the upsert adopts it and the contacts still land), or
    `not_in_scan` (not in this run's results; the page and the row have
    drifted). A partial answer with named outcomes beats failing a batch over
    its one stale member.
    """
    user_uuid, tenant_uuid = _require_session(request)

    if len(body.domains) > settings.CLIENT_DISCOVERY_MAX_DOMAINS:
        # More than a run can even hold — the request is not from our page.
        raise HTTPException(status_code=400, detail="Too many domains at once.")

    outcomes: list[dict] = []
    async with tenant_session(tenant_uuid) as session:
        run = await _latest_run(session, user_uuid)
        if run is None or run.status != ClientDiscoveryRun.DONE:
            raise HTTPException(
                status_code=409, detail="Run a scan first, and let it finish."
            )

        # Serialised per tenant, so a double-clicked Create (or a Create
        # racing a colleague's scan) cannot insert the same contact twice —
        # `client_contacts` deliberately has no unique index on the address.
        await lock_contact_application(session, tenant_uuid)

        entries = {entry["domain"]: entry for entry in (run.results or [])}
        created_domains: set[str] = set()
        for domain in dict.fromkeys(body.domains):
            entry = entries.get(domain)
            if entry is None:
                outcomes.append({"domain": domain, "outcome": "not_in_scan"})
                continue
            client_id, created, added = await create_client_from_entry(
                session, tenant_uuid, entry
            )
            outcomes.append(
                {
                    "domain": domain,
                    "outcome": "created" if created else "already_existed",
                    "client_id": str(client_id),
                    "contacts_added": added,
                }
            )
            created_domains.add(domain)

        if created_domains:
            # Re-assigned, not mutated in place: JSONB change tracking only
            # sees assignment. The flag is what lets the page mark rows done
            # after a reload rather than offering them again.
            run.results = [
                {
                    **entry,
                    "created": bool(entry.get("created")) or entry["domain"] in created_domains,
                }
                for entry in (run.results or [])
            ]

    log.info(
        "client_discovery_clients_created",
        tenant_id=str(tenant_uuid),
        created=sum(1 for o in outcomes if o["outcome"] == "created"),
        requested=len(body.domains),
    )
    return {"results": outcomes}
