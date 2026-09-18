"""Contact provider seam — the paid half of Reveal Contact.

Spec: `serper design.md` §26 (contact enrichment), §10/§41 (provider as a seam).

Why a slot rather than an integration: verified contact data (a direct-dial
number, a personal address) is a **vendor product** — ContactOut, RocketReach,
Apollo, Lusha. This slice ships the seam and the honest empty answer, so the
feature is real end-to-end today (public emails work) and a vendor is a
configuration change plus one class, not a re-architecture.

The one rule: **a provider's answer is never presented as ours.** `provider` is
returned on every response so the UI can attribute it, and `verified` is the
provider's claim about its own data, not something this module asserts.

`NoopProvider` is the default (`CONTACT_PROVIDER="none"`). It makes no network
call, costs nothing, and answers with a sentence naming the vendors a recruiter
could connect — an empty result that explains itself rather than a silent
nothing that looks broken.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# The vendor setup sentence. Shown verbatim by the panel, so it lives here next
# to the provider it describes, not in the route.
NO_PROVIDER_MESSAGE = (
    "Connect a contact provider (ContactOut/RocketReach/Apollo) to reveal "
    "verified contacts."
)

# Recognised provider keys → the not-yet-shipped vendor. `none` is the Noop.
CONTACT_PROVIDER_NONE = "none"


@dataclass
class EnrichmentResult:
    """One provider's answer, in our shape.

    `status` is `ok` when the provider returned data, `no_provider` when none is
    configured, and a provider's own error status otherwise ('not_found',
    'unauthorized', …). `verified` describes the *provider's* data: a Noop
    answer carries no contacts, so it is trivially `False`.
    """

    status: str
    provider: str
    emails: list[dict] = field(default_factory=list)
    phones: list[dict] = field(default_factory=list)
    verified: bool = False
    message: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "provider": self.provider,
            "emails": self.emails,
            "phones": self.phones,
            "verified": self.verified,
            "message": self.message,
        }


class ContactProvider(Protocol):
    """The seam (§26). One method today; `health()`/`estimate_cost()` (§41) land
    when a caller needs them, the same reading `WebSearchProvider` takes.

    `canonical` is the resolved identity (`canonical_profile_url`, name, company,
    title, location) — the minimum a vendor needs to disambiguate a person. It
    is passed as a plain dict so a provider never imports the resolver.
    """

    name: str

    async def enrich(self, canonical: dict) -> EnrichmentResult: ...


class NoopProvider:
    """The default: no vendor configured, no network call, an honest answer.

    Returns `no_provider` so the route stores a distinguishable value and the
    panel can render the setup sentence. This is deliberately *not* an error:
    the deployment is not broken, it just has no paid contact source connected.
    """

    name = CONTACT_PROVIDER_NONE

    async def enrich(self, canonical: dict) -> EnrichmentResult:  # noqa: ARG002
        return EnrichmentResult(
            status="no_provider",
            provider=self.name,
            message=NO_PROVIDER_MESSAGE,
        )


def get_provider(name: str | None = None) -> ContactProvider:
    """Resolve the configured provider (settings.CONTACT_PROVIDER).

    Unknown keys log once and fall back to the Noop, so a typo in an env var
    degrades to the honest empty answer rather than a 500 on every click — the
    same fail-open reading `serper_configured()` takes. Adding a vendor is a new
    branch here plus its class; no route changes.
    """
    key = (name if name is not None else settings.CONTACT_PROVIDER).strip().lower()
    if key in ("", CONTACT_PROVIDER_NONE):
        return NoopProvider()
    log.warning("contact_provider_unknown", provider=key)
    return NoopProvider()
