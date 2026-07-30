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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.api.auth import _require_session
from app.api.integrity import is_duplicate
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.client import Client, ClientContact, ClientMention
from app.services.client_naming import normalize_company_name

router = APIRouter(tags=["clients"])

StatusFilter = Literal["unconfirmed", "confirmed", "suspended", "archived", "merged"]


class MergeRequest(BaseModel):
    target_id: uuid.UUID


class _ClientFieldRules:
    """Validation shared by the create and patch bodies.

    The pipeline never stores a free-provider domain — `domain_of` returns None
    for anything in `settings.FREE_EMAIL_DOMAINS`, because such a domain
    identifies a person rather than a company. This API must not be the back
    door that puts one in.
    """

    @field_validator("email_domain", check_fields=False)
    @classmethod
    def _domain_is_a_company(cls, value: str | None) -> str | None:
        # None is meaningful and allowed: on create it means "no domain known",
        # on patch it means "clear the one we got wrong".
        if value is None:
            return None
        cleaned = value.strip().lower().lstrip("@")
        if not cleaned:
            return None
        if cleaned in settings.FREE_EMAIL_DOMAINS:
            raise ValueError(
                f"{cleaned} is a free email provider and identifies a person, "
                "not a company; leave the domain unset"
            )
        return cleaned

    @field_validator("name", check_fields=False)
    @classmethod
    def _name_is_not_blank(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class ClientCreate(_ClientFieldRules, BaseModel):
    """Only `name` is required — everything else is a fact one may not have yet."""

    name: str
    email_domain: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0)
    notes: str | None = None


class ClientUpdate(_ClientFieldRules, BaseModel):
    """Every field optional — this is a PATCH.

    Reusing `ClientCreate` would make `PATCH {"notes": "..."}` a 422 for
    omitting `name`. `exclude_unset=True` on the dump is what keeps "not sent"
    different from "set to null", and that distinction only exists because
    every field may be absent here.
    """

    name: str | None = None
    email_domain: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0)
    notes: str | None = None


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
        "website": client.website,
        "phone": client.phone,
        "address": client.address,
        "fee_percent": float(client.fee_percent) if client.fee_percent is not None else None,
        "payment_terms_days": client.payment_terms_days,
        "notes": client.notes,
        "source": client.source,
        "suspended_reason": client.suspended_reason,
        "suspended_at": client.suspended_at.isoformat() if client.suspended_at else None,
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
        # chip appears to ask. "all" agrees with what the unfiltered list
        # shows — a merged row is no longer a client, so it is excluded from
        # "all" exactly as it is excluded from the default listing below,
        # while still being counted (and reachable) under its own status.
        counts = {"all": 0}
        for stored, n in await session.execute(
            select(Client.status, func.count()).group_by(Client.status)
        ):
            if stored != Client.MERGED:
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

        contacts = (
            (
                await session.execute(
                    select(ClientContact)
                    .where(ClientContact.client_id == client_id)
                    # Primary first, then oldest first — a stable order, so two
                    # readers of the same client see the same list.
                    .order_by(ClientContact.is_primary.desc(), ClientContact.created_at)
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
    payload["contacts"] = [_serialize_contact(c) for c in contacts]
    return payload


async def _lock_pair(session, first: uuid.UUID, second: uuid.UUID) -> None:
    """Take a row lock on both clients, lowest id first.

    One statement per row rather than an `IN (...)` with an ORDER BY: the order
    in which Postgres locks the rows of a single statement is a property of the
    chosen plan, not of the ORDER BY, so only separate statements make the
    ordering something this code actually decides. A missing row simply locks
    nothing — the 404 comes from `_load` afterwards.
    """
    for client_id in sorted((first, second), key=lambda value: value.bytes):
        await session.execute(
            select(Client.id).where(Client.id == client_id).with_for_update()
        )


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


async def _domain_conflict(tenant_uuid: uuid.UUID, domain: str) -> HTTPException:
    """Build the 409 for a domain that is already spoken for.

    The IntegrityError does not carry the holder, and the transaction that
    raised it is rolled back, so the holder is read in a fresh session. If it
    has since gone (a merge in between), the message degrades to naming the
    domain alone rather than inventing a client.
    """
    async with tenant_session(tenant_uuid) as session:
        holder = (
            await session.execute(
                select(Client).where(
                    Client.email_domain == domain, Client.status != Client.MERGED
                )
            )
        ).scalar_one_or_none()
    if holder is None:
        return HTTPException(status_code=409, detail=f"The domain {domain} is already in use")
    return HTTPException(
        status_code=409,
        detail=(
            f"{holder.name} ({holder.id}) already holds {domain} and is {holder.status}. "
            "Open that client and edit it instead of adding a second one."
        ),
    )


@router.post("/clients", status_code=201)
async def create_client(request: Request, body: ClientCreate) -> dict:
    """Add a client by hand, at `confirmed`.

    The pipeline's rows start at `unconfirmed` because a domain match is not a
    fact about which company an email is from. A recruiter typing the name is
    that judgement being made, so asking them to confirm it afterwards would
    be asking them to agree with themselves.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    client_id = uuid.uuid4()
    values = body.model_dump()
    values.update(
        id=client_id,
        tenant_id=tenant_uuid,
        name_normalized=normalize_company_name(body.name),
        status=Client.CONFIRMED,
        source=Client.MANUAL,
    )

    async with tenant_session(tenant_uuid) as session:
        try:
            await session.execute(insert(Client).values(**values))
            await session.commit()
        except IntegrityError as exc:
            # `uq_clients_tenant_domain` is the only constraint these values
            # can violate with a 23505: `id` is server-side generated here and
            # `status` is fixed. Anything else is a bug in this endpoint and is
            # re-raised rather than disguised as a collision.
            await session.rollback()
            if not is_duplicate(exc) or body.email_domain is None:
                raise
            raise await _domain_conflict(tenant_uuid, body.email_domain) from exc

    return await get_client(request, client_id)


@router.patch("/clients/{client_id}")
async def update_client(request: Request, client_id: uuid.UUID, body: ClientUpdate) -> dict:
    """Edit the facts a recruiter owns. Status is not among them.

    Every status change has its own endpoint, because each one is a decision
    with its own legal sources; letting PATCH write `status` would route around
    all of them.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    # exclude_unset, so an omitted field is left alone and an explicit null
    # clears the column. Those are different requests and must stay different.
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await get_client(request, client_id)
    if "name" in changes:
        changes["name_normalized"] = normalize_company_name(changes["name"])

    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status == Client.MERGED:
            # Editing a merged row would edit something no longer in the list,
            # and an unmerge would then resurrect an edit nobody remembers.
            raise HTTPException(status_code=400, detail="Unmerge the client first")
        try:
            await session.execute(update(Client).where(Client.id == client_id).values(**changes))
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if not is_duplicate(exc) or not changes.get("email_domain"):
                raise
            raise await _domain_conflict(tenant_uuid, changes["email_domain"]) from exc

    return await get_client(request, client_id)


@router.post("/clients/{client_id}/confirm")
async def confirm_client(request: Request, client_id: uuid.UUID) -> dict:
    """Accept a proposed client as one the agency really works with.

    Only from `unconfirmed` (or `confirmed`, idempotently). An archived client
    must go through `restore` first: archiving is a decision that this company
    is no longer current, and confirming straight out of it would reinstate the
    row as live without it ever passing back through review. `merged` is
    refused too — unmerge first. See `_LEGAL_SOURCES`.
    """
    return await _transition(request, client_id, Client.CONFIRMED)


@router.post("/clients/{client_id}/archive")
async def archive_client(request: Request, client_id: uuid.UUID) -> dict:
    # Clears any suspension: a reason that outlived the state it described
    # would show a recruiter a hold on a client that is simply archived.
    return await _transition(request, client_id, Client.ARCHIVED, dict(_CLEAR_SUSPENSION))


@router.post("/clients/{client_id}/restore")
async def restore_client(request: Request, client_id: uuid.UUID) -> dict:
    """Undo an archive — and only an archive.

    Unlike a candidate (`active | archived | merged`), a client also has
    `confirmed`, and confirmation is a human judgement. Restoring a
    `confirmed` row would silently demote it back into the review queue and
    erase that judgement, so this refuses anything but `archived` rather than
    reusing the generic `_transition` (which only guards against `merged`).
    A merged row is refused for the same reason archive refuses one: unmerge
    must come first so `status` and `merged_into_client_id` never disagree
    about whether the row is live.

    Restoring lands back in `unconfirmed`, not the pre-archive status: a
    client archived long ago is deliberately sent through review again
    rather than reinstated as confirmed by an endpoint call. This is a
    choice, not an oversight — remembering the pre-archive status would need
    a new column, and re-review is cheap for a recruiter to redo.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status != Client.ARCHIVED:
            raise HTTPException(
                status_code=400,
                detail=f"Client is {client.status}, not archived; nothing to restore",
            )
        await session.execute(
            update(Client).where(Client.id == client_id).values(status=Client.UNCONFIRMED)
        )
        await session.commit()
    return {"status": Client.UNCONFIRMED}


@router.post("/clients/{client_id}/merge")
async def merge_client(request: Request, client_id: uuid.UUID, body: MergeRequest) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    if body.target_id == client_id:
        raise HTTPException(status_code=400, detail="A client cannot be merged into itself")

    async with tenant_session(tenant_uuid) as session:
        # Lock both rows before reading their statuses. Without this, merging
        # A→B and B→A at the same instant leaves the two rows pointing at each
        # other: both validated against a snapshot taken before the other
        # wrote, both committed, and neither row is live or unmergeable back
        # into one. Lowest id first so the two transactions queue instead of
        # deadlocking, and the statuses are read only once both locks are held.
        await _lock_pair(session, client_id, body.target_id)
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
        # target already has a mention for the same email_message_id, the
        # repoint would collide with uq_client_mentions_once_per_message
        # (NULLS NOT DISTINCT, so two NULL-message mentions collide too).
        #
        # A real (non-null) message id collision means one message mentioning
        # one company only needs one mention on the surviving client — the
        # weaker of the two is redundant and is dropped. Strength is judged
        # by matched_by alone (email_domain is a firmer claim than a
        # normalised-name match, so it outranks it): there is no confidence
        # column to break a tie on, and matched_by never ties between two
        # different values by construction. Two mentions that share the same
        # matched_by are equally strong evidence, so on that genuine tie the
        # target's existing mention is kept and the loser's is dropped as
        # redundant — which side wins doesn't matter, since neither claim is
        # stronger than the other.
        #
        # A NULL message id collision is different: the constraint permits
        # only one NULL-message mention per client, but a NULL id does not
        # mean "same missing source" — it means the source was retention-
        # purged, and two purged mentions are two different purged emails.
        # They are not duplicates of each other and neither is redundant, so
        # neither is moved or deleted; the loser's NULL-message mention stays
        # on the loser row exactly as it is. The loser row is not deleted by
        # a merge (status just becomes `merged`), it stays reachable by id,
        # and its mentions survive there — "the source is gone" stays true
        # without ever becoming "this never happened" on either row.
        def _strength(mention: ClientMention) -> int:
            return {"email_domain": 2, "name": 1}.get(mention.matched_by, 0)

        target_mentions = (
            (
                await session.execute(
                    select(ClientMention).where(ClientMention.client_id == body.target_id)
                )
            )
            .scalars()
            .all()
        )
        target_by_message = {
            m.email_message_id: m for m in target_mentions if m.email_message_id is not None
        }
        loser_mentions = (
            (
                await session.execute(
                    select(ClientMention).where(ClientMention.client_id == client_id)
                )
            )
            .scalars()
            .all()
        )

        movable_ids = []
        redundant_loser_ids = []
        outranked_target_ids = []
        for m in loser_mentions:
            if m.email_message_id is None:
                # Two purged sources, not one duplicated source — leave in place.
                continue
            collision = target_by_message.get(m.email_message_id)
            if collision is None:
                movable_ids.append(m.id)
            elif _strength(m) > _strength(collision):
                outranked_target_ids.append(collision.id)
                movable_ids.append(m.id)
            else:
                redundant_loser_ids.append(m.id)

        if outranked_target_ids:
            # Free the (client_id, email_message_id) slot before the winning
            # loser mention is repointed into it below.
            await session.execute(
                delete(ClientMention).where(ClientMention.id.in_(outranked_target_ids))
            )
        if movable_ids:
            await session.execute(
                update(ClientMention)
                .where(ClientMention.id.in_(movable_ids))
                .values(client_id=body.target_id)
            )
        if redundant_loser_ids:
            await session.execute(
                delete(ClientMention).where(ClientMention.id.in_(redundant_loser_ids))
            )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(
                status=Client.MERGED,
                merged_into_client_id=body.target_id,
                **_CLEAR_SUSPENSION,
            )
        )
        await session.commit()
    return {"status": "merged", "merged_into_client_id": str(body.target_id)}


@router.post("/clients/{client_id}/unmerge")
async def unmerge_client(request: Request, client_id: uuid.UUID) -> dict:
    """Restore a merged client. The mentions the merge moved stay with the target.

    Deliberately partial: a moved mention carries no record of which client it
    came from, so it cannot be given back. What does return is the evidence the
    merge never moved — mentions whose source email has been purged, which are
    kept on this row precisely because they cannot be deduplicated. Re-ingestion
    re-attaches anything still arriving.
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


# Which statuses each transition may be made *from*. Stated as a whitelist
# rather than a list of the statuses to refuse: excluding only `merged` let an
# archived client be confirmed directly, which walked straight past `restore` —
# the endpoint that exists precisely so an archived client re-enters the review
# queue rather than becoming live again in one call.
#
# Confirming is a human judgement about a client the agency currently works
# with, so it may only be made about a row that is in (or already past) review,
# never about one that was put away. Archiving is always available except from
# `merged`, where `unmerge` must come first so `status` and
# `merged_into_client_id` never disagree about whether the row is live. Both
# transitions stay idempotent — re-confirming a confirmed row is a no-op, not
# an error, because a double-clicked button is not a mistake worth an error.
_LEGAL_SOURCES: dict[str, frozenset[str]] = {
    # `suspended` is deliberately absent here: unsuspend is the only exit from
    # a suspension, and `confirm` special-cases it below with a message that
    # names that endpoint rather than `restore`.
    Client.CONFIRMED: frozenset({Client.UNCONFIRMED, Client.CONFIRMED}),
    # Only a confirmed client can be put on hold. A client that was never
    # confirmed is not one the agency has said it works with, and putting that
    # away is what `archive` is for.
    Client.SUSPENDED: frozenset({Client.CONFIRMED, Client.SUSPENDED}),
    # From `suspended` too: a hold that becomes permanent should not need an
    # unsuspend hop first.
    Client.ARCHIVED: frozenset(
        {Client.UNCONFIRMED, Client.CONFIRMED, Client.SUSPENDED, Client.ARCHIVED}
    ),
}


async def _transition(
    request: Request,
    client_id: uuid.UUID,
    status: str,
    extra: dict | None = None,
) -> dict:
    """Move a client between statuses, optionally writing other columns with it.

    `extra` exists because suspension is not status alone: `suspend` sets the
    reason and timestamp, `unsuspend` and `archive` clear them. Doing that in
    the same UPDATE is what makes it impossible for a stale reason to outlive
    the state it describes — two statements could be interrupted between them.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        # `_LEGAL_SOURCES` already excludes `merged` from every transition, so
        # this guard is redundant for correctness — it exists only to answer
        # with "unmerge first" instead of the generic "restore it before
        # marking it ..." message a merged row would otherwise get from the
        # whitelist check below.
        if client.status == Client.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the client first")
        if client.status not in _LEGAL_SOURCES[status]:
            # A suspended row reached through `confirm` would otherwise be told
            # to `restore` — an endpoint that refuses anything but `archived`,
            # so the caller would be sent to a second error. Name the endpoint
            # that actually works, exactly as the merged guard above does.
            if client.status == Client.SUSPENDED:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsuspend the client first, then mark it {status}",
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Client is {client.status}; restore it before marking it {status}"
                ),
            )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=status, **(extra or {}))
        )
        await session.commit()
    return {"status": status}


class SuspendRequest(BaseModel):
    """A reason is optional but wanted.

    Nothing invents one when it is absent (§15) — the row simply records that
    the client is on hold without saying why, which is the truth.
    """

    reason: str | None = None


_CLEAR_SUSPENSION = {"suspended_at": None, "suspended_reason": None}


@router.post("/clients/{client_id}/suspend")
async def suspend_client(
    request: Request, client_id: uuid.UUID, body: SuspendRequest
) -> dict:
    """Put a live client on hold — an unpaid invoice, a contract dispute.

    Distinct from `archive`, which says the agency no longer works with this
    company at all. A suspended client stays in the list and keeps its domain,
    and the matcher goes on attaching its mail; what stops is submitting
    candidates to it (see `record_submission` in app/api/sourcing.py).
    """
    return await _transition(
        request,
        client_id,
        Client.SUSPENDED,
        {"suspended_at": datetime.now(UTC), "suspended_reason": body.reason},
    )


@router.post("/clients/{client_id}/unsuspend")
async def unsuspend_client(request: Request, client_id: uuid.UUID) -> dict:
    """Lift a hold, landing back on `confirmed`.

    Deliberately unlike `restore`, which lands on `unconfirmed`. Archiving
    revoked the judgement that the agency currently works with this firm, so
    re-review is the point. A suspension revoked nothing — the agency still
    works with the firm, it simply was not sending candidates — so sending the
    row back through review would be noise.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status != Client.SUSPENDED:
            raise HTTPException(
                status_code=400,
                detail=f"Client is {client.status}, not suspended; nothing to lift",
            )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=Client.CONFIRMED, **_CLEAR_SUSPENSION)
        )
        await session.commit()
    return {"status": Client.CONFIRMED}


class ContactIn(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    is_primary: bool = False


class ContactUpdate(_ClientFieldRules, BaseModel):
    """`name` reuses `_ClientFieldRules._name_is_not_blank`: a contact's name is

    a NOT NULL column exactly like a client's, so an explicit `{"name": null}`
    must be a 422 naming the field rather than reach the UPDATE and 500.
    """

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    is_primary: bool | None = None


def _serialize_contact(contact: ClientContact) -> dict:
    return {
        "id": str(contact.id),
        "name": contact.name,
        "email": contact.email,
        "phone": contact.phone,
        "title": contact.title,
        "is_primary": contact.is_primary,
        "created_at": contact.created_at.isoformat(),
    }


async def _demote_primary(session, client_id: uuid.UUID, except_id: uuid.UUID | None) -> None:
    """Clear the existing primary before another row claims the title.

    Order is the whole safety mechanism. `uq_client_contacts_one_primary` is a
    partial unique INDEX, and an index cannot be DEFERRABLE — Postgres has no
    partial unique constraint to defer. A unique index is checked at the end of
    each statement, so demote-then-promote never has two `true` rows visible to
    a check; promote-then-demote fails mid-transaction every time.
    """
    statement = update(ClientContact).where(
        ClientContact.client_id == client_id, ClientContact.is_primary.is_(True)
    )
    if except_id is not None:
        statement = statement.where(ClientContact.id != except_id)
    await session.execute(statement.values(is_primary=False))


async def _load_contact(session, client_id: uuid.UUID, contact_id: uuid.UUID) -> ClientContact:
    """404 for a contact that is not this client's, for the same reason `_load`
    404s across tenants: an id that exists but is not yours is a disclosure."""
    contact = (
        await session.execute(
            select(ClientContact).where(
                ClientContact.id == contact_id, ClientContact.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.post("/clients/{client_id}/contacts", status_code=201)
async def create_contact(request: Request, client_id: uuid.UUID, body: ContactIn) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    contact_id = uuid.uuid4()
    async with tenant_session(tenant_uuid) as session:
        # Loaded, not trusted: another agency's client id is a 404 here rather
        # than a foreign key violation later.
        await _load(session, client_id)
        if body.is_primary:
            await _demote_primary(session, client_id, except_id=None)
        contact = ClientContact(
            id=contact_id,
            tenant_id=tenant_uuid,
            client_id=client_id,
            **body.model_dump(),
        )
        session.add(contact)
        # Flushed and refreshed within the same transaction, not committed and
        # re-queried: `tenant_session` sets `app.tenant_id` with SET LOCAL, so
        # it is scoped to this one transaction and would vanish from under a
        # second commit's fresh transaction, turning the reload into a
        # cross-tenant read that RLS answers with zero rows.
        await session.flush()
        await session.refresh(contact)
        return _serialize_contact(contact)


@router.patch("/clients/{client_id}/contacts/{contact_id}")
async def update_contact(
    request: Request, client_id: uuid.UUID, contact_id: uuid.UUID, body: ContactUpdate
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    changes = body.model_dump(exclude_unset=True)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, client_id)
        contact = await _load_contact(session, client_id, contact_id)
        if changes.get("is_primary"):
            await _demote_primary(session, client_id, except_id=contact_id)
        if changes:
            await session.execute(
                update(ClientContact).where(ClientContact.id == contact_id).values(**changes)
            )
        await session.flush()
        await session.refresh(contact)
        return _serialize_contact(contact)


@router.delete("/clients/{client_id}/contacts/{contact_id}", status_code=204)
async def delete_contact(request: Request, client_id: uuid.UUID, contact_id: uuid.UUID) -> None:
    """Deleted, not archived — unlike everything else in this module.

    A contact is not evidence that something happened; it is a current fact
    about who to call. A stale one is worse than an absent one.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, client_id)
        await _load_contact(session, client_id, contact_id)
        await session.execute(delete(ClientContact).where(ClientContact.id == contact_id))
