"""The scope configuration must match what the environment actually declares.

This file exists because it did not. `.env` was split into `MS_IDENTITY_SCOPES`
and `MS_MAILBOX_SCOPES` while `config.py` still read a single `MS_GRAPH_SCOPES`
that no longer existed. It fell back to `""`, so every sign-in requested no
permissions at all — and nothing failed, because Entra issues an ID token
regardless. The damage would have surfaced much later, as a 403 on the first
mailbox read.

Consent is deliberately incremental: identity at sign-in, mailbox access only
when a mailbox is connected (see docs/setup.md). Both halves are asserted here,
because the failure mode of each is silent.
"""

import pytest

from app.core.config import Settings, settings
from app.services.ms_auth import delegated_scopes

REQUIRED_KEYS = ("MS_IDENTITY_SCOPES", "MS_MAILBOX_SCOPES")


def test_the_settings_declare_the_keys_the_environment_uses():
    declared = set(Settings.model_fields)

    for key in REQUIRED_KEYS:
        assert key in declared, f"{key} is used but Settings does not declare it"

    assert "MS_GRAPH_SCOPES" not in declared, (
        "the single-key form is gone from the environment; a Settings field "
        "that nothing sets would silently fall back to an empty scope list"
    )


def test_every_scope_key_is_set_somewhere():
    """The keys must be set, whether by `.env` locally or env vars in CI.

    Asserted on the resolved settings rather than the file, because the file is
    only one of the two ways they arrive — and an assertion that passed merely
    because the file was absent would hide the exact drift this file exists to
    catch. `.github/workflows/backend.yml` sets both for CI.
    """
    unset = [key for key in REQUIRED_KEYS if not getattr(settings, key).strip()]

    assert unset == [], (
        f"{unset} resolved empty. Set them in .env (local) or the workflow env "
        "block (CI) — an empty scope list still signs in successfully, so "
        "nothing else will tell you."
    )


def test_identity_scopes_are_not_empty():
    """An empty list still signs in successfully, which is what makes it
    dangerous: the failure surfaces only when a mailbox is read."""
    assert settings.identity_scopes


def _mail_permissions(scopes) -> list[str]:
    """Graph mailbox permissions among `scopes`.

    Matched on the `Mail.` prefix rather than the substring "mail", because the
    OIDC `email` scope contains that substring and is not a mailbox
    permission — it discloses an address, not a message.
    """
    return [s for s in scopes if s.lower().startswith("mail.")]


def test_mailbox_scopes_grant_mail_read():
    """Graph requires Mail.Read to subscribe to message change notifications.

    It is also the least-privileged permission that works, so anything broader
    would be a product-promise violation (§6.1), not just an over-ask.
    """
    assert any(scope.lower() == "mail.read" for scope in settings.mailbox_scopes)


def test_mailbox_scopes_stay_read_only():
    """Read-only access is a product promise (§6.1)."""
    forbidden = [s for s in settings.mailbox_scopes if "write" in s.lower() or "send" in s.lower()]

    assert forbidden == [], f"mailbox consent must stay read-only, got {forbidden}"


def test_signing_in_never_asks_for_mail():
    """The point of the split: a user who only signs in — including a
    Google-only user's colleague — is never shown a 'read your mail' prompt for
    a capability they have not asked for."""
    assert _mail_permissions(delegated_scopes("identity")) == []


def test_the_mailbox_consent_asks_for_mail_and_nothing_else():
    mailbox = delegated_scopes("mailbox")

    assert _mail_permissions(mailbox)
    assert not any(scope.lower() == "user.read" for scope in mailbox), (
        "identity scopes are already consented; re-asking widens the prompt "
        "for no gain"
    )


@pytest.mark.parametrize("kind", ["identity", "mailbox"])
def test_reserved_scopes_are_stripped_from_every_consent(kind):
    """MSAL injects openid/profile/offline_access itself and errors if they are
    passed in, even though the app registration genuinely holds them."""
    scopes = {s.lower() for s in delegated_scopes(kind)}

    assert not scopes & {"openid", "profile", "offline_access"}


def test_an_unknown_scope_kind_is_rejected():
    """A typo must not silently produce an empty list — that is the exact bug
    this file was written to catch."""
    with pytest.raises(ValueError):
        delegated_scopes("everything")
