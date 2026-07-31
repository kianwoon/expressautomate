"""Saying which client a job order came from.

Every row this exists to fix is unassigned, which is why the permission rule
here is "editable OR unassigned" rather than `can_edit` alone — `can_edit`
deliberately refuses unassigned rows, and gating on it would make the endpoint
unable to solve its own problem.
"""

import uuid

import httpx
import pytest

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.main import app
from app.models import Client, Opportunity, User
from app.models.opportunity_share import OpportunityShare
from app.services.notify.events import EVENT_OPPORTUNITY_ASSIGNED
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user


def _signed_in_client(user_id: uuid.UUID, tenant_id: uuid.UUID) -> httpx.AsyncClient:
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    )
    c.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )
    return c


async def _second_user(
    tenant_id: uuid.UUID,
    role: str = "recruiter",
    preferred_name: str | None = None,
    display_name: str | None = None,
) -> uuid.UUID:
    other = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            User(
                id=other,
                tenant_id=tenant_id,
                email=f"{other.hex[:8]}@agency.sg",
                role=role,
                preferred_name=preferred_name,
                display_name=display_name,
            )
        )
        await s.commit()
    return other


async def _opportunity(
    tenant_id: uuid.UUID, assigned_user_id: uuid.UUID | None = None
) -> uuid.UUID:
    """A job order with no email behind it — this file is about the client
    link, not extraction, and `email_message_id` is nullable for that case."""
    opportunity_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
                assigned_user_id=assigned_user_id,
                company_name_raw="Acme Pte Ltd",
                job_title_raw="Java Developer",
                location_raw="Singapore",
                salary_raw="SGD 6,000",
            )
        )
        await s.commit()
    return opportunity_id


async def _client_row(
    tenant_id: uuid.UUID,
    assigned_user_id: uuid.UUID | None = None,
    name: str = "Acme Pte Ltd",
) -> uuid.UUID:
    client_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Client(
                id=client_id,
                tenant_id=tenant_id,
                name=name,
                name_normalized=name.lower(),
                assigned_user_id=assigned_user_id,
            )
        )
        await s.commit()
    return client_id


async def _share_with(
    opportunity_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    async with AdminSessionLocal() as s:
        s.add(
            OpportunityShare(
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                scope=OpportunityShare.SCOPE_USER,
                shared_with_user_id=user_id,
            )
        )
        await s.commit()


async def _row(opportunity_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                Opportunity.__table__.select().where(
                    Opportunity.__table__.c.id == opportunity_id
                )
            )
        ).one()


async def _post(
    tenant_id: uuid.UUID, user_id: uuid.UUID, opportunity_id: uuid.UUID, body: dict
) -> httpx.Response:
    async with _signed_in_client(user_id, tenant_id) as c:
        return await c.post(f"/api/opportunities/{opportunity_id}/client", json=body)


@pytest.fixture
def captured_events(monkeypatch) -> list:
    """What the route asked to be sent, without a notification catalogue.

    Patched on `app.api.opportunities` because the route imported the name.
    """
    events: list = []

    async def _capture(event) -> int:
        events.append(event)
        return 0

    monkeypatch.setattr("app.api.opportunities.emit_and_enqueue", _capture)
    return events


async def test_linking_a_client_adopts_its_recruiter() -> None:
    # Unassigned job order + a client assigned to Wei Kian -> the job order
    # becomes Wei Kian's, and the response names them.
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        wei_kian = await _second_user(tenant_id, preferred_name="Wei Kian")
        client_id = await _client_row(tenant_id, assigned_user_id=wei_kian)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": str(opportunity_id),
            "client_id": str(client_id),
            "client_name": "Acme Pte Ltd",
            "assigned_user_id": str(wei_kian),
            "assignee_name": "Wei Kian",
        }
        row = await _row(opportunity_id)
        assert row.client_id == client_id
        assert row.assigned_user_id == wei_kian
    finally:
        await cleanup_tenant(tenant_id)


async def test_adopt_false_leaves_it_on_the_queue() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        wei_kian = await _second_user(tenant_id, preferred_name="Wei Kian")
        client_id = await _client_row(tenant_id, assigned_user_id=wei_kian)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id,
            user_id,
            opportunity_id,
            {"client_id": str(client_id), "adopt_client_recruiter": False},
        )

        assert response.status_code == 200, response.text
        assert response.json()["assigned_user_id"] is None
        assert response.json()["assignee_name"] is None
        row = await _row(opportunity_id)
        assert row.client_id == client_id
        assert row.assigned_user_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_client_with_no_recruiter_leaves_it_unassigned() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        client_id = await _client_row(tenant_id, assigned_user_id=None)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 200, response.text
        assert response.json()["assigned_user_id"] is None
        row = await _row(opportunity_id)
        assert row.client_id == client_id
        assert row.assigned_user_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_assigned_job_order_never_changes_hands() -> None:
    """Linking a client is not a way to take someone else's work."""
    # Assigned to A; A links a client owned by B; assignee stays A even with
    # adopt_client_recruiter=true.
    tenant_id, user_a = await seed_tenant_with_user()
    try:
        user_b = await _second_user(tenant_id, preferred_name="Wei Kian")
        client_id = await _client_row(tenant_id, assigned_user_id=user_b)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=user_a)

        response = await _post(
            tenant_id,
            user_a,
            opportunity_id,
            {"client_id": str(client_id), "adopt_client_recruiter": True},
        )

        assert response.status_code == 200, response.text
        assert response.json()["assigned_user_id"] == str(user_a)
        row = await _row(opportunity_id)
        assert row.assigned_user_id == user_a
        assert row.client_id == client_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_the_assignee_may_set_the_client_on_their_own_job_order() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        client_id = await _client_row(tenant_id, assigned_user_id=None)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=user_id)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 200, response.text
        assert (await _row(opportunity_id)).client_id == client_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_bystander_is_refused_and_writes_nothing() -> None:
    """A permission check placed after the update would leave the damage done."""
    # B's assigned job order, C calls the route -> 403 AND client_id unchanged
    # in the database.
    tenant_id, user_b = await seed_tenant_with_user()
    try:
        user_c = await _second_user(tenant_id)
        original = await _client_row(tenant_id, name="Original Pte Ltd")
        other_client = await _client_row(tenant_id, name="Other Pte Ltd")
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=user_b)
        async with AdminSessionLocal() as s:
            await s.execute(
                Opportunity.__table__.update()
                .where(Opportunity.__table__.c.id == opportunity_id)
                .values(client_id=original)
            )
            await s.commit()
        # Shared with C, so the row is visible to them: without this the
        # refusal would be the visibility guard's 404 and prove nothing.
        await _share_with(opportunity_id, tenant_id, user_c)

        response = await _post(
            tenant_id, user_c, opportunity_id, {"client_id": str(other_client)}
        )

        assert response.status_code == 403, response.text
        row = await _row(opportunity_id)
        assert row.client_id == original
        assert row.assigned_user_id == user_b
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_invisible_job_order_is_404_not_403() -> None:
    """A 403 would confirm the row exists."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        other = await _second_user(tenant_id)
        client_id = await _client_row(tenant_id)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=other)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "No such job order."
        assert (await _row(opportunity_id)).client_id is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_null_unlinks_and_does_not_touch_the_assignee() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        client_id = await _client_row(tenant_id, assigned_user_id=user_id)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=user_id)
        async with AdminSessionLocal() as s:
            await s.execute(
                Opportunity.__table__.update()
                .where(Opportunity.__table__.c.id == opportunity_id)
                .values(client_id=client_id)
            )
            await s.commit()

        response = await _post(tenant_id, user_id, opportunity_id, {"client_id": None})

        assert response.status_code == 200, response.text
        assert response.json()["client_id"] is None
        assert response.json()["assigned_user_id"] == str(user_id)
        row = await _row(opportunity_id)
        assert row.client_id is None
        assert row.assigned_user_id == user_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_adoption_notifies_the_new_owner(captured_events) -> None:
    """A job order quietly becoming yours is what the assigned event is for."""
    # Linking a client whose recruiter is B, on an unassigned job order, emits
    # EVENT_OPPORTUNITY_ASSIGNED naming only B.
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        user_b = await _second_user(tenant_id)
        client_id = await _client_row(tenant_id, assigned_user_id=user_b)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 200, response.text
        assert len(captured_events) == 1, captured_events
        event = captured_events[0]
        assert event.kind == EVENT_OPPORTUNITY_ASSIGNED
        # Only them: `None` here would mean the whole agency.
        assert event.recipient_user_ids == (user_b,)
        assert event.opportunity_id == opportunity_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_linking_without_adoption_notifies_nobody(captured_events) -> None:
    # Nothing changed hands, so there is nothing to announce.
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        user_b = await _second_user(tenant_id)
        client_id = await _client_row(tenant_id, assigned_user_id=user_b)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id,
            user_id,
            opportunity_id,
            {"client_id": str(client_id), "adopt_client_recruiter": False},
        )

        assert response.status_code == 200, response.text
        assert captured_events == []
    finally:
        await cleanup_tenant(tenant_id)


async def test_assignee_name_falls_back_to_email_local_part() -> None:
    """A whitespace-only `preferred_name` must fall through, not win.

    `_assignee_name_expr` wraps each candidate in `nullif(btrim(...), '')`
    precisely so this happens; a plain `COALESCE` over the raw columns would
    return the blank string instead of reaching the email fallback.
    """
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        wei_kian = await _second_user(
            tenant_id, preferred_name="   ", display_name=None
        )
        async with AdminSessionLocal() as s:
            row = (
                await s.execute(
                    User.__table__.select().where(User.__table__.c.id == wei_kian)
                )
            ).one()
        local_part = row.email.split("@", 1)[0]

        client_id = await _client_row(tenant_id, assigned_user_id=wei_kian)
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 200, response.text
        assert response.json()["assignee_name"] == local_part
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_client_from_another_agency_is_refused() -> None:
    # 422 before the composite FK can turn it into a 500 — the same guard
    # `assign_opportunity` puts on its user target.
    tenant_a, user_a = await seed_tenant_with_user()
    tenant_b, _user_b = await seed_tenant_with_user()
    try:
        foreign_client = await _client_row(tenant_b)
        opportunity_id = await _opportunity(tenant_a, assigned_user_id=None)

        response = await _post(
            tenant_a, user_a, opportunity_id, {"client_id": str(foreign_client)}
        )

        assert response.status_code == 422, response.text
        assert (await _row(opportunity_id)).client_id is None
    finally:
        await cleanup_tenant(tenant_a)
        await cleanup_tenant(tenant_b)


async def test_the_response_names_the_client_it_just_linked() -> None:
    """So the panel can show what it did without reading the row back.

    The same reason `assignee_name` is here: the browser holds an id and no
    name, and a second request to turn one into the other is a request the
    panel would be rendering an empty picker during.
    """
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        client_id = await _client_row(tenant_id, name="Sunrise Logistics")
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)

        response = await _post(
            tenant_id, user_id, opportunity_id, {"client_id": str(client_id)}
        )

        assert response.status_code == 200, response.text
        assert response.json()["client_name"] == "Sunrise Logistics"
    finally:
        await cleanup_tenant(tenant_id)


async def test_unlinking_reports_no_client_name() -> None:
    """`null` is the answer, not the previous name left behind."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        client_id = await _client_row(tenant_id, name="Sunrise Logistics")
        opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
        await _post(tenant_id, user_id, opportunity_id, {"client_id": str(client_id)})

        response = await _post(tenant_id, user_id, opportunity_id, {"client_id": None})

        assert response.status_code == 200, response.text
        assert response.json()["client_id"] is None
        assert response.json()["client_name"] is None
    finally:
        await cleanup_tenant(tenant_id)
