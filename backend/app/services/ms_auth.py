"""Microsoft Entra ID sign-in — a thin wrapper over MSAL (plan §6.1).

MSAL owns state, nonce and PKCE generation and validates the returned id_token,
so nothing here reimplements any part of OAuth. The only job of this module is
to feed MSAL values from settings and hand back the flow dict and token result.

Identity and mailbox ingestion are two consents, not one. They were a single
flow until a tenant with restricted user consent refused the whole thing —
mailbox access is not a permission an ordinary user may grant there, so
bundling it made plain sign-in need an administrator. Sign-in now asks only for
identity; the mailbox scopes are requested later, by someone who has decided to
connect a mailbox, and the refresh token from *that* consent is what lets the
worker read Outlook mail.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from functools import lru_cache

import msal
from msal.authority import AZURE_PUBLIC
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.crypto import decrypt, encrypt
from app.db.rls import tenant_session
from app.models.ms_token import MicrosoftToken

# MSAL adds these itself and raises ValueError if they are passed in, even
# though they are exactly what `.env` lists as the app registration's scopes.
_MSAL_RESERVED_SCOPES = frozenset({"openid", "profile", "offline_access"})


def authority() -> str:
    """Authority for the configured Entra tenant.

    `MS_TENANT_ID` is `common` for this multi-tenant registration, so the
    authority is the same for every agency and for personal accounts alike;
    only the `tid` claim tells them apart.
    """
    return f"https://{AZURE_PUBLIC}/{settings.MS_TENANT_ID}"


def delegated(scopes: list[str]) -> list[str]:
    """`scopes` with the ones MSAL insists on adding itself removed."""
    return [s for s in scopes if s.lower() not in _MSAL_RESERVED_SCOPES]


def identity_scopes() -> list[str]:
    """Sign-in. Deliberately no mailbox permission — see config.py."""
    return delegated(settings.identity_scopes)


def mailbox_scopes() -> list[str]:
    """Connecting a mailbox: the identity scopes plus the mailbox ones.

    Both, not just the new one: incremental consent returns a token for exactly
    what was asked, so naming only the mailbox scope would hand back a token
    narrower than the one already held.
    """
    return delegated(settings.graph_scopes)


@lru_cache(maxsize=1)
def client() -> msal.ConfidentialClientApplication:
    """One long-lived client; MSAL caches Entra's OIDC metadata on it."""
    return msal.ConfidentialClientApplication(
        settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=authority(),
    )


def begin_login(scopes: list[str], prompt: str | None = None) -> dict:
    """Start the auth-code flow for `scopes`.

    The scope set is a parameter rather than a constant because sign-in and
    connecting a mailbox ask for different things — see config.py.

    The returned dict carries the PKCE verifier, state and nonce; it must reach
    `complete_login` unmodified or MSAL rejects the response.

    `prompt` is the OIDC parameter of the same name, passed straight through to
    the authorize request. Without it Microsoft silently reuses whichever
    account already has a browser SSO session, so someone signed in as one
    account can never reach a second one — `select_account` is what forces the
    picker. It is not defaulted here: paying the extra click on every ordinary
    sign-in is a worse trade than letting the caller ask for it.
    """
    return client().initiate_auth_code_flow(
        delegated(scopes), redirect_uri=settings.MS_REDIRECT_URI, prompt=prompt
    )


def complete_login(flow: dict, auth_response: dict) -> dict:
    """Exchange the code. MSAL validates state, nonce and the id_token itself."""
    return client().acquire_token_by_auth_code_flow(flow, auth_response)


class MailboxNotAuthorised(Exception):
    """The stored grant can no longer read this mailbox.

    Distinct from a transient failure: the fix is the user reconnecting, not a
    retry. Callers mark the mailbox `needs_reauth` and stop.
    """


# allow-hardcode: SQL statements, not a phrase list — no behaviour is keyed on
# matching these strings against anything.
#
# The owner is looked up separately from the token, so the lock can be taken on
# the user *before* the token is read. Reading first and locking second would
# leave both racers holding the same pre-rotation token.
_MAILBOX_OWNER = text("SELECT user_id FROM mailboxes WHERE id = :mailbox_id")

_LOCK_PER_USER = text("SELECT pg_advisory_xact_lock(hashtext(:key))")

_GRANT_FOR_USER = text(
    "SELECT refresh_token_encrypted FROM ms_oauth_tokens WHERE user_id = :user_id"
)


async def access_token_for_mailbox(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> str:
    """Exchange the stored refresh token for a mailbox-scoped access token.

    Serialized per user with a transaction-scoped advisory lock. Entra rotates
    refresh tokens on use, so two jobs refreshing the same user concurrently
    would otherwise both read the pre-rotation token; the one that lost the
    race then presents a token Entra has already replaced, and the mailbox gets
    flagged `needs_reauth` for a grant that is perfectly healthy.

    Order matters: the lock is taken **before** the token is read, so the
    waiter re-reads whatever the winner persisted. Locking after the read would
    serialize the refresh while still handing both racers the same stale token,
    which is the appearance of safety without any of it.

    That re-read depends on READ COMMITTED, Postgres' default, where each
    statement takes a fresh snapshot. Under REPEATABLE READ the waiter's second
    SELECT would still see the pre-lock snapshot and the bug returns — silently,
    because the lock would still look correct. Anything that changes the
    isolation level for this session has to revisit this function.

    Being transaction-scoped, the lock cannot outlive a crashed worker.
    """
    async with tenant_session(tenant_id) as session:
        owner = (
            await session.execute(_MAILBOX_OWNER, {"mailbox_id": mailbox_id})
        ).scalar_one_or_none()
        if owner is None:
            # The mailbox outlived the user who connected it — `user_id` is
            # SET NULL on delete, so nobody is left who can authorise a read.
            raise MailboxNotAuthorised(f"mailbox {mailbox_id} has no owner")

        return await _token_for_user(
            session, tenant_id, owner, absent=f"no stored grant for mailbox {mailbox_id}"
        )


async def access_token_for_user(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """The same exchange, for a user who has no mailbox row yet.

    The preview step (§6.2) has to read Graph *before* anything is provisioned —
    that is the whole point of showing someone their inbox before importing it.
    It shares this path rather than reimplementing it so that the rotation lock,
    which is the part that is easy to get subtly wrong, has exactly one
    implementation.
    """
    async with tenant_session(tenant_id) as session:
        return await _token_for_user(
            session, tenant_id, user_id, absent=f"no stored grant for user {user_id}"
        )


async def _token_for_user(session, tenant_id: uuid.UUID, owner: uuid.UUID, *, absent: str) -> str:
    """Lock, read, refresh, persist — in that order. See the caller's docstring."""
    await session.execute(_LOCK_PER_USER, {"key": f"ms-refresh:{owner}"})

    encrypted = (
        await session.execute(_GRANT_FOR_USER, {"user_id": owner})
    ).scalar_one_or_none()
    if encrypted is None:
        raise MailboxNotAuthorised(absent)

    result = await asyncio.to_thread(
        client().acquire_token_by_refresh_token,
        decrypt(encrypted),
        # The full mailbox set — identity scopes included. Asking for
        # only the new permission returns a token narrower than the grant
        # already held, which is why `mailbox_scopes()` unions them.
        scopes=mailbox_scopes(),
    )
    if "access_token" not in result:
        raise MailboxNotAuthorised(result.get("error_description", "refresh token rejected"))

    if result.get("refresh_token"):
        await store_refresh_token(
            session,
            tenant_id=tenant_id,
            user_id=owner,
            home_account_id=None,
            result=result,
            now=datetime.now(UTC),
        )
    return result["access_token"]


async def store_refresh_token(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    home_account_id: str | None,
    result: dict,
    now: datetime,
) -> None:
    """Persist a refresh token rotated during a background refresh.

    Only for the worker's own refresh cycle. The consent flows write through
    `_store_token_if_not_a_downgrade` in `app/api/auth.py`, which compares
    scope sets under a row lock — a plain upsert here would defeat that guard
    and could quietly replace a mailbox-capable grant with a narrower one.

    This path is safe to write unconditionally because it re-requests
    `mailbox_scopes()`, the widest set the application ever asks for, so what
    comes back is never narrower than what was stored.

    A response without a refresh token is not an error. MSAL omits it when the
    cached grant still applies, and the stored one remains valid.
    """
    refresh_token = result.get("refresh_token")
    if not refresh_token:
        return

    ciphertext = encrypt(refresh_token)
    await session.execute(
        pg_insert(MicrosoftToken)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            # MSAL's own account key format: "<oid>.<tid>".
            home_account_id=home_account_id,
            refresh_token_encrypted=ciphertext,
            scope=result.get("scope"),
        )
        .on_conflict_do_update(
            constraint="uq_ms_tokens_tenant_user",
            set_={
                "refresh_token_encrypted": ciphertext,
                "scope": result.get("scope"),
                "updated_at": now,
            },
        )
    )
