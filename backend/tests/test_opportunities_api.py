"""The job-order list a recruiter reads (plan §16, §17).

Three things this endpoint must never get wrong, one of which is a security
property:

- Agency A must not see Agency B's vacancies. The list is the product; a leak
  here hands a competitor's live roles to a competitor.
- Newest first, because the received date is the column the spreadsheet this
  replaces never had, and an unsorted list makes it decorative.
- A field the email did not mention stays null. Substituting "" or 0 makes an
  absence indistinguishable from an extracted value, which is the fabrication
  §15 forbids — and the UI renders it as data.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models import Opportunity, User
from tests.conftest import AdminSessionLocal

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def settings_the_suite_supplies(monkeypatch) -> None:
    """CI has no `.env`, so the suite states every value it depends on.

    Unconditional rather than a fallback: relying on the developer machine's
    `.env` for the limit would test a different number locally than in CI.
    """
    monkeypatch.setattr(settings, "OPPORTUNITIES_PAGE_LIMIT", 200)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """ASGI transport, not TestClient: TestClient drives its own event loop and
    the engine in app.db.session is pinned to the session-scoped one."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
async def seeded():
    """Two agencies, each with one mailbox, and a factory for their vacancies.

    Seeded through the admin role because RLS is the thing under test: fixtures
    written through the restricted role would prove isolation by never having
    inserted the other tenant's rows in the first place.
    """
    tenants: list[uuid.UUID] = []

    async def make_tenant(slug: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        tenant_id, user_id, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with AdminSessionLocal() as s:
            await s.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
                {"i": tenant_id, "n": slug, "s": f"{slug}-{tenant_id.hex[:8]}"},
            )
            # The ORM here, not raw SQL: `users.role` is NOT NULL with a
            # Python-side default, which a hand-written INSERT never fires —
            # and naming a role literally would freeze this fixture to a value
            # the model owns.
            s.add(User(id=user_id, tenant_id=tenant_id, email=f"{tenant_id.hex[:8]}@{slug}.sg"))
            # Flushed before the mailbox INSERT: raw SQL does not autoflush, so
            # without this the FK sees a user that has not been written yet.
            await s.flush()
            await s.execute(
                text(
                    "INSERT INTO mailboxes"
                    " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                    " VALUES (:i, :t, :u, :m, 'user', 'inbox', :r)"
                ),
                {
                    "i": mailbox_id,
                    "t": tenant_id,
                    "u": user_id,
                    "m": f"oid-{tenant_id.hex[:8]}",
                    "r": settings.DEFAULT_RETENTION_MONTHS,
                },
            )
            await s.commit()
        tenants.append(tenant_id)
        return tenant_id, user_id, mailbox_id

    async def make_opportunity(
        tenant_id: uuid.UUID, mailbox_id: uuid.UUID, **fields
    ) -> uuid.UUID:
        email_id, opportunity_id = uuid.uuid4(), uuid.uuid4()
        received = fields.pop("received_datetime", NOW)
        async with AdminSessionLocal() as s:
            await s.execute(
                text(
                    "INSERT INTO email_messages"
                    " (id, tenant_id, mailbox_id, graph_message_id, received_datetime)"
                    " VALUES (:i, :t, :m, :g, :r)"
                ),
                {
                    "i": email_id,
                    "t": tenant_id,
                    "m": mailbox_id,
                    "g": f"graph-{email_id.hex}",
                    "r": received,
                },
            )
            # The ORM again, for `review_status` and `quality_state`: both are
            # NOT NULL with Python-side defaults, and a fixture that named them
            # would be asserting against values it had chosen itself.
            s.add(
                Opportunity(
                    id=opportunity_id,
                    tenant_id=tenant_id,
                    email_message_id=email_id,
                    received_datetime=received,
                    **fields,
                )
            )
            await s.commit()
        return opportunity_id

    yield make_tenant, make_opportunity

    for tid in tenants:
        async with tenant_session(tid) as s:
            await s.execute(text("DELETE FROM opportunities"))
            await s.execute(text("DELETE FROM email_messages"))
            await s.execute(text("DELETE FROM mailboxes"))
            await s.execute(text("DELETE FROM users"))
            await s.execute(text("DELETE FROM tenants"))


def sign_in(client: httpx.AsyncClient, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """The cookie the OAuth callback would have set, without the OAuth."""
    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )


async def test_one_agency_never_sees_another_agencys_vacancies(client, seeded) -> None:
    """The security property. Everything else on this page is cosmetic beside it."""
    make_tenant, make_opportunity = seeded
    tenant_a, user_a, mailbox_a = await make_tenant("agency-a")
    tenant_b, _user_b, mailbox_b = await make_tenant("agency-b")
    await make_opportunity(tenant_a, mailbox_a, company_name_raw="Acme Pte Ltd")
    await make_opportunity(tenant_b, mailbox_b, company_name_raw="Rival Holdings")

    sign_in(client, user_a, tenant_a)
    body = (await client.get("/api/opportunities")).json()

    companies = [row["company_name_raw"] for row in body["opportunities"]]
    assert companies == ["Acme Pte Ltd"]
    assert "Rival Holdings" not in companies


async def test_newest_first(client, seeded) -> None:
    """The received date is the column the spreadsheet lacked; ordering is what
    makes it worth having."""
    make_tenant, make_opportunity = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Older", received_datetime=NOW - timedelta(days=5)
    )
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Newest", received_datetime=NOW)
    await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Middle", received_datetime=NOW - timedelta(days=1)
    )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    assert [row["company_name_raw"] for row in body["opportunities"]] == [
        "Newest",
        "Middle",
        "Older",
    ]


async def test_a_field_the_email_did_not_mention_stays_null(client, seeded) -> None:
    """No empty strings, no zeros. The UI reads null as "Not mentioned" (§15);
    a substituted value would be rendered as though the email said it."""
    make_tenant, make_opportunity = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd")

    sign_in(client, user_id, tenant_id)
    row = (await client.get("/api/opportunities")).json()["opportunities"][0]

    for field in (
        "salary_raw",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "working_hours_raw",
        "requirements",
        "duration_raw",
        "location_raw",
        "job_title_raw",
        "job_description",
    ):
        assert row[field] is None, f"{field} was filled in with {row[field]!r}"


async def test_the_job_description_reaches_the_screen(client, seeded) -> None:
    """The table has a column for it, so the payload has to carry it.

    Asserted separately from the null case because the two failures look
    nothing alike: a missing key 404s the column for every row, while a
    substituted "" would render as a job with no description written.
    """
    make_tenant, make_opportunity = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme Pte Ltd",
        job_description="Manage the front desk and greet visitors.",
    )

    sign_in(client, user_id, tenant_id)
    row = (await client.get("/api/opportunities")).json()["opportunities"][0]

    assert row["job_description"] == "Manage the front desk and greet visitors."


async def test_an_anonymous_caller_gets_nothing(client) -> None:
    """No session, no list — the guard is the endpoint's, not the browser's."""
    assert (await client.get("/api/opportunities")).status_code == 401
