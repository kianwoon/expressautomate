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

from functools import lru_cache

import msal
from msal.authority import AZURE_PUBLIC

from app.core.config import settings

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
