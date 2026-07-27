"""Microsoft Entra ID sign-in — a thin wrapper over MSAL (plan §6.1).

MSAL owns state, nonce and PKCE generation and validates the returned id_token,
so nothing here reimplements any part of OAuth. The only job of this module is
to feed MSAL values from settings and hand back the flow dict and token result.

Identity and mailbox access arrive on **two** consents, not one. Signing in asks
only for `MS_IDENTITY_SCOPES`; `Mail.Read` is requested separately when a user
connects a mailbox, so nobody is asked to hand over their mail before they have
asked for mail ingestion — and a Google-only agency's colleagues never see that
prompt at all.

Entra's consent is cumulative per user and application, so the refresh token
stored after the mailbox grant covers both sets, and it is that token the
ingestion worker later exchanges for a mail-capable access token.
"""

import uuid
from datetime import datetime
from functools import lru_cache

import msal
from msal.authority import AZURE_PUBLIC
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.crypto import encrypt
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


def delegated_scopes(kind: str = "identity") -> list[str]:
    """Scopes to request for one consent step.

    Consent is incremental — identity at sign-in, mailbox access only when a
    mailbox is connected — so which set is wanted has to be said explicitly.
    An unknown `kind` raises rather than returning an empty list: a silent
    empty list is exactly how the `MS_GRAPH_SCOPES` drift went unnoticed, since
    Entra happily issues an ID token for no scopes at all.
    """
    if kind == "identity":
        requested = settings.identity_scopes
    elif kind == "mailbox":
        requested = settings.mailbox_scopes
    else:
        raise ValueError(f"unknown scope kind: {kind!r}")
    return [s for s in requested if s.lower() not in _MSAL_RESERVED_SCOPES]


@lru_cache(maxsize=1)
def client() -> msal.ConfidentialClientApplication:
    """One long-lived client; MSAL caches Entra's OIDC metadata on it."""
    return msal.ConfidentialClientApplication(
        settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=authority(),
    )


def begin_login() -> dict:
    """Start the auth-code flow.

    The returned dict carries the PKCE verifier, state and nonce; it must reach
    `complete_login` unmodified or MSAL rejects the response.
    """
    return client().initiate_auth_code_flow(
        delegated_scopes("identity"), redirect_uri=settings.MS_REDIRECT_URI
    )


def complete_login(flow: dict, auth_response: dict) -> dict:
    """Exchange the code. MSAL validates state, nonce and the id_token itself."""
    return client().acquire_token_by_auth_code_flow(flow, auth_response)


async def store_refresh_token(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    home_account_id: str,
    result: dict,
    now: datetime,
) -> None:
    """Upsert one user's encrypted refresh token.

    Both consent steps land here — sign-in and, later, mailbox connection —
    because a second copy of a token-encryption path is a second place to get
    it wrong. Overwriting is correct rather than lossy: Entra's consent is
    cumulative per user and app, so a token minted after the mailbox grant
    covers strictly more than the one it replaces.

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
