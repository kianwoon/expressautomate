"""Deterministic pre-gate noise filter — the cheapest gate of all: free.

A recruiter's inbox is mostly not job orders, and most of that non-job-order
mail is *obviously* not a job order to a human: newsletters, no-reply
notifications, job alerts from portals, bounces, invoices. Paying a model to
reach the same "not a job order" a subject line already answers is the exact
waste the LLM gate exists to reduce.

This module answers the easy negatives with rules and configuration, before
the LLM gate is ever called. The contract is deliberately one-directional:

  - A rule can answer `non_recruitment` (definitely noise) and nothing else.
  - A rule can never answer `recruitment`. If the rules are unsure, the email
    proceeds to the LLM gate exactly as before.

That asymmetry preserves the gate's fail-open safety (a missed rule costs a
few hundred tokens; a wrong `recruitment` from a rule would be a fabricated
answer, which the product forbids). It also means this filter can only ever
*subtract* from the LLM bill, never add a call.

Rules are configuration-first, matching `client_discovery`:
  - `NOISE_SUBJECT_MARKERS`: substrings that make a subject an obvious
    non-job-order (newsletter, job alert, notification, etc.).
  - `NOISE_LOCALPART_MARKERS`: sender local-parts like `no-reply`,
    `newsletter`, `bounce`.
  - `CLIENT_DISCOVERY_EXCLUDED_DOMAINS` is reused: a domain the discovery
    scan already excludes (portals, mass mailers) is not a job-order source.

`gate_rule()` returns None when no rule fires — the caller then runs the LLM
gate. It never raises: a misconfigured settings list is a gate call, not a
crash.
"""

from app.core.config import settings
from app.services.client_naming import domain_of

# The pseudo-model recorded on a rule-answered verdict. Reports can tell the
# free answers from the paid ones, exactly like TRUSTED_SENDER_MODEL.
GATE_RULE_MODEL = "gate-rule"

# allow-hardcode: these are the well-known shapes of inbox noise, deliberately
# mirrored here rather than only in client_discovery so the gate stays cheap.
# Each is config-overridable via NOISE_SUBJECT_MARKERS / NOISE_LOCALPART_MARKERS.
_DEFAULT_SUBJECT_MARKERS = (
    "newsletter",
    "job alert",
    "job digest",
    "daily digest",
    "weekly digest",
    "notification",
    "unsubscribe",
    "you're invited",
    "you are invited",
    "meeting invite",
    "calendar invite",
    "invoice",
    "payment due",
    "receipt",
    "statement",
    "welcome to",
    "verify your",
    "reset your password",
    "security alert",
    "2-step verification",
    "otp",
    "bounce",
    "delivery failed",
    "undeliverable",
    "out of office",
    "automatic reply",
    "auto-reply",
    "do not reply",
    "do not respond",
)

# Sender local-parts that mark an automated/mass sender rather than a client.
_DEFAULT_LOCALPART_MARKERS = (
    "no-reply",
    "noreply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "newsletter",
    "notifications",
    "notification",
    "mailer",
    "mail",
    "alerts",
    "alert",
    "bounce",
    "bounces",
    "postmaster",
    "mailer-daemon",
    "mailerdaemon",
    "automated",
    "auto",
    "info",
    "support",
    "help",
    "account",
    "billing",
    "invoice",
)


def _subject_markers() -> tuple[str, ...]:
    raw = getattr(settings, "NOISE_SUBJECT_MARKERS", "")
    if isinstance(raw, str) and raw.strip():
        return tuple(m.strip().lower() for m in raw.split(",") if m.strip())
    return _DEFAULT_SUBJECT_MARKERS


def _localpart_markers() -> tuple[str, ...]:
    raw = getattr(settings, "NOISE_LOCALPART_MARKERS", "")
    if isinstance(raw, str) and raw.strip():
        return tuple(m.strip().lower() for m in raw.split(",") if m.strip())
    return _DEFAULT_LOCALPART_MARKERS


def gate_rule(
    *,
    subject: str | None,
    sender_email: str | None,
) -> tuple[str, str] | None:
    """Return `(status, reason)` when a rule knows the answer, else None.

    Only `non_recruitment` is ever returned. None means "not sure — run the
    LLM gate". Never raises: every config path degrades to None.
    """
    if subject:
        low = subject.lower()
        for marker in _subject_markers():
            if marker in low:
                return "non_recruitment", f"subject matches noise marker: {marker!r}"

    if sender_email:
        local = sender_email.split("@", 1)[0].lower()
        for marker in _localpart_markers():
            if marker in local:
                return "non_recruitment", f"sender local-part matches noise marker: {marker!r}"
        domain = domain_of(sender_email)
        if domain and _excluded_domain(domain):
            return "non_recruitment", f"sender domain is on the excluded list: {domain}"

    return None


def _excluded_domain(domain: str) -> bool:
    entries = getattr(settings, "CLIENT_DISCOVERY_EXCLUDED_DOMAINS", frozenset())
    if not entries:
        return False
    return any(domain == e or domain.endswith("." + e) for e in entries)
