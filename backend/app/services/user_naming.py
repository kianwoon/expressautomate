"""Resolve what to call a signed-in user — never fabricated (§15).

Two different resolutions live here, on purpose, because they face two
different audiences:

- `actor_name` is read by colleagues in the same tenant on the activity
  timeline. An email address is an acceptable last resort there: it still
  identifies a real person to someone entitled to see it.
- `recruiter_name` is read by a candidate, a stranger outside the tenant, in
  an outreach message. An email address must never reach that audience —
  handing a candidate a colleague's mailbox address is a bigger leak than an
  imperfect greeting — so the chain ends in `None` instead, and callers
  rewrite the sentence around a missing name rather than printing one.

Both start from `preferred_name`: the one field the user chose themselves,
as opposed to `display_name`, which sign-in overwrites from the Entra/Google
claims on every login (see the upsert in app/api/auth.py).
"""


def actor_name(preferred_name: str | None, display_name: str | None, email: str) -> str:
    """Name for the activity timeline, seen only by colleagues in this tenant."""
    return preferred_name or display_name or email


def recruiter_name(preferred_name: str | None, display_name: str | None) -> str | None:
    """Name for a candidate-facing draft. No email fallback — see module docstring."""
    return preferred_name or display_name or None
