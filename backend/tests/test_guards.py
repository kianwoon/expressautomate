"""The guard that stops the suite writing to a real database.

CI only ever exercises the passing path, so without these the refusal branch
would never run anywhere. It exists because the suite once wrote to production.
"""

from tests.conftest import remote_hosts

LOCAL = "postgresql://u:p@localhost:5432/expressautomate"
CI_SERVICE = "postgresql://u:p@postgres:5432/expressautomate"
PROD = "postgresql://u:p@ep-autumn-pond.c-6.us-east-1.pg.koyeb.app/expressautomate?sslmode=require"


def test_local_urls_are_allowed() -> None:
    assert remote_hosts(LOCAL, LOCAL) == []
    assert remote_hosts(CI_SERVICE, CI_SERVICE) == []


def test_remote_url_is_flagged() -> None:
    assert remote_hosts(PROD, PROD) == ["ep-autumn-pond.c-6.us-east-1.pg.koyeb.app"]


def test_remote_admin_url_is_flagged_even_when_the_app_url_is_local() -> None:
    """The combination that made the original guard insufficient.

    AdminSessionLocal connects via the admin URL and bypasses RLS, so a local
    app URL paired with a production admin URL still deletes real rows.
    """
    assert remote_hosts(LOCAL, PROD) == ["ep-autumn-pond.c-6.us-east-1.pg.koyeb.app"]


def test_empty_and_malformed_urls_do_not_crash_the_guard() -> None:
    assert remote_hosts("", "not-a-url") == []
