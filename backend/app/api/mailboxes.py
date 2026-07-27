"""Connecting a mailbox (plan §6.2).

Routes are declared unprefixed; `app/main.py` mounts them under `/api`.

**Two steps, because consent is incremental.** Signing in grants identity
scopes only — including for anyone who signed in before mailbox scopes
existed — so the stored refresh token cannot read mail. `/connect` starts a
second consent for `MS_MAILBOX_SCOPES` and returns the authorize URL; the work
happens in `/connect/callback` once the user has granted it. Entra's consent is
cumulative per user and app, so the token stored afterwards covers both sets.

Both the subscription and the backfill are queued rather than done inline, so a
large mailbox cannot hold the user's browser on the OAuth redirect. arq gives no
ordering guarantee between them, so the two may overlap or run in either order —
which is fine, and deliberately so: the dedup indexes make overlap free, and any
message that lands in the gap between a finished backfill and a live
subscription is picked up by the ten-minute delta sweep. The recovery layer
exists precisely so onboarding does not need to be a careful sequence.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import text

from app.api.auth import (
    FLOW_TTL_SECONDS,
    _cookie_kwargs,
    _flow_cookie_name,
    _frontend_url,
    _open_flow,
    _seal_flow,
    _stale_flow_cookies,
    current_user,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import User
from app.services.ms_auth import client, delegated_scopes, store_refresh_token
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])

# Graph's well-known name for the Inbox. `whole_inbox` is not a second
# mechanism: message delta is folder-scoped, so it resolves to a folder id like
# any other choice and both scopes share one code path.
WELL_KNOWN_INBOX = "inbox"

# allow-hardcode: SQL statements, not a phrase list.
_INSERT_MAILBOX = text(
    """
    INSERT INTO mailboxes
        (id, tenant_id, user_id, ms_user_id, scope, folder_id, folder_name,
         initial_sync_from, retention_months)
    VALUES (:id, :tenant_id, :user_id, :ms_user_id, :scope, :folder_id,
            :folder_name, :initial_sync_from, :retention_months)
    ON CONFLICT (tenant_id, ms_user_id, folder_id) DO UPDATE SET
        status = 'active',
        user_id = EXCLUDED.user_id,
        initial_sync_from = EXCLUDED.initial_sync_from
    RETURNING id
    """
)
# Reusing the row is what makes reconnection work: the mail already ingested
# stays attached to it, and flipping `status` back to active is precisely what
# a mailbox coming out of `needs_reauth` needs.
#
# Overwriting `initial_sync_from` re-runs the backfill from the newly chosen
# date. That is deliberate and safe rather than merely tolerable: the backfill
# builds a filtered URL and ignores the stored delta checkpoint (see
# `_walk_start`), so an earlier date genuinely reaches further back, and the
# dedup indexes make everything already held a no-op.


class ConnectRequest(BaseModel):
    scope: str
    folder_id: str | None = None
    folder_name: str | None = None
    start_from: datetime

    @model_validator(mode="after")
    def _check(self) -> "ConnectRequest":
        if self.scope not in ("whole_inbox", "folder"):
            raise ValueError("scope must be 'whole_inbox' or 'folder'")
        if self.scope == "folder" and not self.folder_id:
            raise ValueError("folder_id is required when scope is 'folder'")
        return self


def resolve_start_date(requested: datetime) -> datetime:
    """Clamp the requested start to what the implementation can actually do.

    Graph delta filtered by `receivedDateTime` is not a bulk export mechanism,
    so the product must not offer more than it can deliver. A future date is
    pulled back too — otherwise the backfill filters for mail that cannot exist
    and the user sees an empty import with no explanation.
    """
    now = datetime.now(UTC)
    earliest = now - timedelta(days=settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS)
    return min(max(requested, earliest), now)


def _require_microsoft_identity(user: User) -> None:
    """Only a Microsoft identity has a mailbox to connect (§6.1).

    There is no Gmail ingestion path, so a Google-only user has no
    `ms_object_id`. Letting them through would write a row with a NULL
    `ms_user_id` — which never conflicts, so the upsert would duplicate on
    every attempt — and put `None` in the Graph URLs the jobs build.
    """
    if not user.ms_object_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "Mailbox ingestion requires signing in with Microsoft. "
                "Google sign-in does not carry mail access."
            ),
        )


@router.post("/connect")
async def connect(
    payload: ConnectRequest,
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> Response:
    """Start the mailbox consent. Returns the URL the browser must visit.

    Nothing is created here. A user who abandons the consent screen must not
    leave a `mailboxes` row behind for the sweeps to find and try to subscribe
    for, every fifteen minutes, forever.
    """
    if not settings.microsoft_configured():
        raise HTTPException(
            status_code=503, detail="Microsoft sign-in is not configured."
        )
    _require_microsoft_identity(user)

    flow = client().initiate_auth_code_flow(
        delegated_scopes("mailbox"), redirect_uri=settings.MS_MAILBOX_REDIRECT_URI
    )
    # The onboarding choices ride through the OAuth round trip inside the
    # sealed flow, not in the URL — a folder id in a query string is one more
    # thing that can be tampered with on the way back.
    sealed = _seal_flow({**flow, "onboarding": payload.model_dump(mode="json")})

    response = JSONResponse({"authorize_url": flow["auth_uri"]})
    # Named per flow, exactly as sign-in does: two tabs mid-onboarding must not
    # overwrite each other. Returning the sealed value in the body instead
    # would leave the callback with no cookie to read at all.
    response.set_cookie(
        _flow_cookie_name(flow["state"]), sealed, **_cookie_kwargs(FLOW_TTL_SECONDS)
    )
    for stale in _stale_flow_cookies(request):
        response.delete_cookie(stale, path="/")
    return response


@router.get("/connect/callback")
async def connect_callback(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> Response:
    """Finish consent, then create the mailbox, subscription and backfill."""
    state = request.query_params.get("state", "")
    sealed = request.cookies.get(_flow_cookie_name(state))
    if sealed is None:
        # Expired, evicted, or a callback that never had a flow. 400, not the
        # 500 a bare lookup would raise.
        raise HTTPException(status_code=400, detail="Mailbox consent expired. Try again.")

    flow = _open_flow(sealed)
    result = client().acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "refresh_token" not in result:
        raise HTTPException(
            status_code=400,
            detail=result.get("error_description", "Mailbox consent was not granted."),
        )

    # Entra shows an account picker, so the account that consented need not be
    # the one that signed in. Unchecked, a user could consent as somebody
    # else's Microsoft account: that account's refresh token would be stored
    # under this user, while the mailbox row claims this user's `ms_user_id`.
    # Every later Graph call would then run one account's token against the
    # other's mailbox path.
    granted_oid = (result.get("id_token_claims") or {}).get("oid")
    if not granted_oid or granted_oid != user.ms_object_id:
        log.warning(
            "mailbox_consent_account_mismatch",
            user_id=str(user.id),
            granted_oid=granted_oid,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "That Microsoft account is not the one you are signed in with. "
                "Sign in as that account first, or pick the matching one."
            ),
        )

    onboarding = flow.get("onboarding")
    if onboarding is None:
        # A sign-in flow cookie, not a mailbox one — same state, different
        # flow. Reading the key directly would 500 on a request the user can
        # simply retry.
        raise HTTPException(
            status_code=400, detail="That consent did not come from connecting a mailbox."
        )

    payload = ConnectRequest.model_validate(onboarding)
    folder_id = payload.folder_id or WELL_KNOWN_INBOX
    start_from = resolve_start_date(payload.start_from)

    async with tenant_session(user.tenant_id) as session:
        # Overwrites the identity-only token: consent is cumulative, so this
        # one covers strictly more than the token it replaces.
        await store_refresh_token(
            session,
            tenant_id=user.tenant_id,
            user_id=user.id,
            home_account_id=None,
            result=result,
            now=datetime.now(UTC),
        )
        mailbox_id = (
            await session.execute(
                _INSERT_MAILBOX,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": user.tenant_id,
                    "user_id": user.id,
                    "ms_user_id": user.ms_object_id,
                    "scope": payload.scope,
                    "folder_id": folder_id,
                    "folder_name": payload.folder_name,
                    "initial_sync_from": start_from,
                    "retention_months": settings.DEFAULT_RETENTION_MONTHS,
                },
            )
        ).scalar_one()

    # Reconnecting is the same path as connecting: the upsert reactivates the
    # row and this creates a fresh subscription, rather than waiting up to an
    # hour for `ensure_subscriptions` to notice.
    await enqueue(
        "recreate_subscription",
        tenant_id=str(user.tenant_id),
        mailbox_id=str(mailbox_id),
    )
    await enqueue(
        "backfill_mailbox_job",
        tenant_id=str(user.tenant_id),
        mailbox_id=str(mailbox_id),
    )

    log.info(
        "mailbox_connected",
        mailbox_id=str(mailbox_id),
        scope=payload.scope,
        ingesting_from=start_from.isoformat(),
    )

    # The user arrives here from Entra's redirect, in a browser. Returning JSON
    # would leave them looking at a raw object; sign-in redirects for the same
    # reason.
    response = RedirectResponse(_frontend_url("/dashboard"), status_code=303)
    response.delete_cookie(_flow_cookie_name(state), path="/")
    return response
