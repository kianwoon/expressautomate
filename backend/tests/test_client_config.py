"""The free-domain set is configuration, not a literal in the matcher.

A recruiter's own agency is on a real domain; their candidates and some
clients are on gmail.com. Keying a client on a free provider would collapse
every unrelated company into one row, so the set has to exist before the
matcher does — and it has to be a setting, because which providers count is
an operator's judgement and changes without a deploy.
"""

from app.core.config import Settings, settings


def test_free_email_domains_env_var_is_the_one_pydantic_actually_binds(monkeypatch) -> None:
    """`FREE_EMAIL_DOMAINS` is the alias pydantic-settings reads.

    `.env.example` used to document `FREE_EMAIL_DOMAINS_RAW`, which the field
    name would suggest but which the `alias=` binding silently ignores. A
    fresh `Settings()` proves the documented key actually changes the parsed
    value, rather than mutating the module-level singleton other tests share.
    """
    monkeypatch.setenv("FREE_EMAIL_DOMAINS", "onlythis.com")
    fresh = Settings()
    assert fresh.FREE_EMAIL_DOMAINS == frozenset({"onlythis.com"})


def test_free_email_domains_covers_the_common_providers() -> None:
    for provider in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com"):
        assert provider in settings.FREE_EMAIL_DOMAINS


def test_free_email_domains_is_lowercased_and_hashable() -> None:
    assert all(d == d.lower() for d in settings.FREE_EMAIL_DOMAINS)
    assert isinstance(settings.FREE_EMAIL_DOMAINS, frozenset)


def test_clients_page_limit_is_a_positive_int() -> None:
    assert isinstance(settings.CLIENTS_PAGE_LIMIT, int)
    assert settings.CLIENTS_PAGE_LIMIT > 0
