"""The agency's client list — a review queue before it is a directory.

Every row here was proposed by the pipeline and is owned by a person. So the
write endpoints are all state transitions a human makes, and none of them is
something the matcher can do: confirm, archive, merge, unmerge. The matcher
creates and links; it never decides.

`unmerge` exists because merge is destructive to the mention graph and
recruiters will get it wrong. A merge with no way back is a merge people are
afraid to use, and an unused merge leaves the duplicates in the list.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.client import Client, ClientMention

router = APIRouter(tags=["clients"])

StatusFilter = Literal["unconfirmed", "confirmed", "archived", "merged"]


class MergeRequest(BaseModel):
    target_id: uuid.UUID


def _serialize(client: Client) -> dict:
    return {
        "id": str(client.id),
        "name": client.name,
        "name_normalized": client.name_normalized,
        "email_domain": client.email_domain,
        "status": client.status,
        "merged_into_client_id": (
            str(client.merged_into_client_id) if client.merged_into_client_id else None
        ),
        "last_seen_at": client.last_seen_at.isoformat() if client.last_seen_at else None,
        "created_at": client.created_at.isoformat(),
    }


@router.get("/clients")
async def list_clients(
    request: Request,
    # Resolved in the body, not the signature: a default bound at import would
    # freeze the setting at the value it had when the module loaded.
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    status: StatusFilter | None = None,
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    ceiling = settings.CLIENTS_PAGE_LIMIT
    page_limit = ceiling if limit is None else min(limit, ceiling)

    async with tenant_session(tenant_uuid) as session:
        # Counted over the whole tenant, before any filter or window. A count
        # that moved with the page would answer a different question than the
        # chip appears to ask.
        counts = {"all": 0}
        for stored, n in await session.execute(
            select(Client.status, func.count()).group_by(Client.status)
        ):
            counts["all"] += n
            counts[stored] = counts.get(stored, 0) + n

        base = select(Client)
        if status is not None:
            base = base.where(Client.status == status)
        else:
            # A merged row is no longer a client. It stays reachable by id and
            # by explicit filter so an unmerge is still possible.
            base = base.where(Client.status != Client.MERGED)

        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(
                        Client.last_seen_at.desc().nullslast(), Client.created_at.desc()
                    )
                    .limit(page_limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )

    return {
        "items": [_serialize(c) for c in rows],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        "counts": counts,
    }


@router.get("/clients/{client_id}")
async def get_client(request: Request, client_id: uuid.UUID) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        mentions = (
            (
                await session.execute(
                    select(ClientMention)
                    .where(ClientMention.client_id == client_id)
                    .order_by(ClientMention.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    payload = _serialize(client)
    payload["mentions"] = [
        {
            "id": str(m.id),
            "email_message_id": str(m.email_message_id) if m.email_message_id else None,
            "matched_by": m.matched_by,
            "created_at": m.created_at.isoformat(),
        }
        for m in mentions
    ]
    return payload


@router.post("/clients/{client_id}/confirm")
async def confirm_client(request: Request, client_id: uuid.UUID) -> dict:
    return await _transition(request, client_id, Client.CONFIRMED)


@router.post("/clients/{client_id}/archive")
async def archive_client(request: Request, client_id: uuid.UUID) -> dict:
    return await _transition(request, client_id, Client.ARCHIVED)


@router.post("/clients/{client_id}/merge")
async def merge_client(request: Request, client_id: uuid.UUID, body: MergeRequest) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    if body.target_id == client_id:
        raise HTTPException(status_code=400, detail="A client cannot be merged into itself")

    async with tenant_session(tenant_uuid) as session:
        loser = await _load(session, client_id)
        target = await _load(session, body.target_id)
        if target.status == Client.MERGED:
            # Merging into a merged row would build a chain the matcher only
            # follows one hop of. Point at the survivor instead.
            raise HTTPException(
                status_code=400, detail="Target is itself merged; merge into its target"
            )
        if loser.status == Client.MERGED:
            raise HTTPException(status_code=400, detail="Client is already merged")

        # Mentions move, because they are evidence about a company and the
        # company is now the target. Leaving them behind would make the
        # surviving row look newly discovered.
        #
        # A mention cannot simply be repointed with a bare UPDATE: if the
        # target already has a mention for the same email_message_id (e.g.
        # the loser was unmerged and re-attached to the same message by a
        # later reprocess), repointing would collide with
        # uq_client_mentions_once_per_message. That collision is not an
        # error — one message mentioning one company only needs one mention
        # on the surviving client — so the colliding loser mentions are
        # dropped as redundant evidence and only the non-colliding ones move.
        target_message_ids = set(
            (
                await session.execute(
                    select(ClientMention.email_message_id).where(
                        ClientMention.client_id == body.target_id
                    )
                )
            )
            .scalars()
            .all()
        )
        loser_mentions = (
            (
                await session.execute(
                    select(ClientMention).where(ClientMention.client_id == client_id)
                )
            )
            .scalars()
            .all()
        )
        movable_ids = [
            m.id for m in loser_mentions if m.email_message_id not in target_message_ids
        ]
        redundant_ids = [
            m.id for m in loser_mentions if m.email_message_id in target_message_ids
        ]
        if movable_ids:
            await session.execute(
                update(ClientMention)
                .where(ClientMention.id.in_(movable_ids))
                .values(client_id=body.target_id)
            )
        if redundant_ids:
            await session.execute(
                delete(ClientMention).where(ClientMention.id.in_(redundant_ids))
            )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=Client.MERGED, merged_into_client_id=body.target_id)
        )
        await session.commit()
    return {"status": "merged", "merged_into_client_id": str(body.target_id)}


@router.post("/clients/{client_id}/unmerge")
async def unmerge_client(request: Request, client_id: uuid.UUID) -> dict:
    """Restore a merged client. Its mentions stay with the target.

    Deliberately partial: the mentions were rewritten by the merge and there is
    no record of which ones came from where, so returning the row to
    `unconfirmed` with no evidence is the honest outcome. Re-ingestion will
    re-attach anything still arriving.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status != Client.MERGED:
            raise HTTPException(status_code=400, detail="Client is not merged")

        # The merge freed client.email_domain (uq_clients_tenant_domain
        # excludes merged rows), and something else may since have claimed
        # it: a new client created from a later email, or another client
        # merged and unmerged in between. Resurrecting the row unchanged
        # would put two live rows on the same domain and hit that index.
        #
        # Refuse with 409 rather than silently clearing email_domain. A
        # cleared domain is a trap: the recruiter asked to undo a merge and
        # would get back a row that looks restored but has quietly lost the
        # one fact (its domain) that made it identifiable and made future
        # emails match it. A 409 naming the client that now holds the domain
        # is something a recruiter can act on directly — archive or rename
        # that other client, or leave the unmerge undone — with no silent
        # data loss either way.
        if client.email_domain is not None:
            holder = (
                await session.execute(
                    select(Client).where(
                        Client.email_domain == client.email_domain,
                        Client.status != Client.MERGED,
                        Client.id != client_id,
                    )
                )
            ).scalar_one_or_none()
            if holder is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot unmerge: {holder.name} ({holder.id}) now holds "
                        f"the domain {client.email_domain}"
                    ),
                )

        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=Client.UNCONFIRMED, merged_into_client_id=None)
        )
        await session.commit()
    return {"status": Client.UNCONFIRMED}


async def _transition(request: Request, client_id: uuid.UUID, status: str) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status == Client.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the client first")
        await session.execute(update(Client).where(Client.id == client_id).values(status=status))
        await session.commit()
    return {"status": status}


async def _load(session, client_id: uuid.UUID) -> Client:
    """Fetch inside the tenant session, so another agency's id is a 404.

    Not a 403: telling a caller that an id exists but is not theirs is itself
    a cross-tenant disclosure.
    """
    client = (
        await session.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client
