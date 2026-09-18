"""Public-email extraction — the free, honest half of Reveal Contact.

Spec: `serper design.md` §26 (contact enrichment prefers the professional
profile), §48 (provenance — never store a value without its source).

What this module is, and is not:

- **It is a text scraper over search results we already have.** The Serper
  titles/snippets and the resolved page URLs are the only inputs; this slice
  never fetches a page server-side. A snippet that quotes `jane@acme.com` is
  evidence that the address is *publicly listed*, no more.
- **It is not verification.** Every returned email carries `verified: False`
  verbatim, because nothing here proves the address belongs to the candidate or
  that it is live. The UI labels them "Publicly listed — unverified" for the
  same reason. Verified contacts are a vendor's job (`contact_provider.py`).
- **It is deterministic and offline.** Same inputs, same output, no network, so
  it is safe to run inside `resolve()` and unit-test without a provider.

Noise addresses are rejected rather than surfaced (§15's "never fabricate"
applied to contacts): a `noreply@` mailbox, an image filename that merely looks
like an email, or a placeholder `example.com` address is worse than nothing —
it invites a recruiter to send a real message into a void.
"""

import re

# One address, conservative: a local part of the usual characters, an `@`, a
# dotted domain, a 2+ letter TLD. Deliberately not RFC 5322 — the input is
# Serper prose, not a mail header, and a permissive pattern mostly harvests
# punctuation artifacts (".png", trailing dots) from snippets.
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
)

# A `mailto:` prefix sometimes survives into a snippet.
_MAILTO_RE = re.compile(r"^mailto:", re.IGNORECASE)

# §15 contact honesty: automated mailboxes must never be offered as a person's
# contact. Prefixes only — a real address at a real firm can share a domain
# with a `noreply@`, which is why rejection keys on the local part for these.
_ROLE_LOCAL_PARTS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "donotrespond",
        "do-not-respond",
        "bounce",
        "bounces",
        "mailer-daemon",
        "postmaster",
        "webmaster",
        "unsubscribe",
        "abuse",
    }
)

# Placeholder / never-real domains. `example.com` is RFC 2606 reserved, and the
# `.test` TLD is reserved too — an address there is a fixture that leaked, not a
# contact.
_NOISE_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "domain.com",
        "email.com",
        "yourdomain.com",
        "localhost",
        "sentry.io",
        "wixpress.com",
    }
)

# Image / asset extensions: a snippet that writes "logo@2x.png" is a filename,
# not an address. Rejected on the domain's final label.
_ARTIFACT_SUFFIXES = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "ico"})

# §26: at most this many addresses per resolution — a page that lists a whole
# company directory must not flood the modal with strangers' addresses.
MAX_EMAILS = 5

# 254 is the SMTP maximum path length; anything longer is a concatenation
# artifact from the snippet, never a real address.
_MAX_LENGTH = 254


def _is_noise(email: str) -> bool:
    """Whether `email` is a role mailbox, a placeholder or a filename artifact."""
    if len(email) > _MAX_LENGTH:
        return True
    local, _, domain = email.partition("@")
    if not local or not domain:
        return True
    if local.lower() in _ROLE_LOCAL_PARTS:
        return True
    domain = domain.lower()
    if domain in _NOISE_DOMAINS:
        return True
    if domain == "test" or domain.startswith("test.") or domain.endswith(".test"):
        return True
    # A reserved `.test`/`.invalid`/`.localhost` TLD is never routable.
    tld = domain.rsplit(".", 1)[-1]
    if tld in ("test", "invalid", "localhost", "local"):
        return True
    if tld in _ARTIFACT_SUFFIXES:
        return True
    return False


def extract_public_emails(
    snippets: list[str],
    urls: list[str],
) -> list[dict]:
    """Publicly listed emails found in `snippets`, attributed to `urls`.

    `snippets` and `urls` are **index-aligned** — `snippets[i]` is the text of
    the result whose URL is `urls[i]` — which is how `resolve()` records them
    while looping over results. When there is no URL at an index, the address
    is still returned with an empty `source_url` rather than dropped: the
    address was on the page, we simply have no link to cite.

    Returns at most `MAX_EMAILS` items, in first-seen order:

        {"email": "jane@acme.com", "source_url": "https://…", "verified": False}

    Dedupe is on the lowercased address; the first source URL that named it
    wins, so the citation is stable across runs.
    """
    out: list[dict] = []
    seen: set[str] = set()

    for index, text in enumerate(snippets):
        if not text:
            continue
        source_url = urls[index] if index < len(urls) else ""
        for raw in EMAIL_RE.findall(text):
            email = _MAILTO_RE.sub("", raw).strip().lower()
            # Trailing punctuation the character class can swallow.
            email = email.rstrip(".,;:)]}>")
            if not email or email in seen or _is_noise(email):
                continue
            seen.add(email)
            out.append(
                {"email": email, "source_url": source_url, "verified": False}
            )
            if len(out) >= MAX_EMAILS:
                return out
    return out
