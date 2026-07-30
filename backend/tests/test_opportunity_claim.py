"""Moving work between people: claiming, assigning, and typing one in by hand.

The race test is the reason this file exists. Two recruiters press *claim* on
the same unassigned job order at the same moment; exactly one may win, and the
mechanism that decides is `WHERE assigned_user_id IS NULL` inside the UPDATE,
not a read followed by a write. To prove that, the two claims must genuinely
run at once — so each gets its own `AsyncClient` (its own cookie jar, and its
own connection out of the pool), and they are driven with `asyncio.gather`.
A single shared client would serialise them and the test would pass against a
check-and-set implementation that is plainly racy.
"""

import asyncio
import uuid

import httpx
import pytest

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.main import app
from app.models import Client, Opportunity, User
from app.services.notify.events import (
    EVENT_OPPORTUNITY_ASSIGNED,
    EVENT_OPPORTUNITY_NEW,
)
from tests import test_opportunities_api as _api
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user

# The same client/sign-in helpers the rest of the job-order suite uses.
client = _api.client
sign_in = _api.sign_in


def _signed_in_client(user_id: uuid.UUID, tenant_id: uuid.UUID) -> httpx.AsyncClient:
    """A client of its own, so two of them can be in flight at once."""
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


async def _second_user(tenant_id: uuid.UUID, role: str = "recruiter") -> uuid.UUID:
    other = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            User(
                id=other,
                tenant_id=tenant_id,
                email=f"{other.hex[:8]}@agency.sg",
                role=role,
            )
        )
        await s.commit()
    return other


async def _opportunity(
    tenant_id: uuid.UUID, assigned_user_id: uuid.UUID | None = None
) -> uuid.UUID:
    """A job order with no email behind it — this file is about assignment,
    not extraction, and `email_message_id` is nullable for exactly that case."""
    opportunity_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
                assigned_user_id=assigned_user_id,
                company_name_raw="Acme Pte Ltd",
                job_title_raw="Java Developer",
            )
        )
        await s.commit()
    return opportunity_id


async def _claim(
    tenant_id: uuid.UUID, user_id: uuid.UUID, opportunity_id: uuid.UUID
) -> httpx.Response:
    async with _signed_in_client(user_id, tenant_id) as c:
        return await c.post(f"/api/opportunities/{opportunity_id}/claim")


async def _assigned_user_id(opportunity_id: uuid.UUID) -> uuid.UUID | None:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                Opportunity.__table__.select().where(
                    Opportunity.__table__.c.id == opportunity_id
                )
            )
        ).one().assigned_user_id


@pytest.fixture
def captured_events(monkeypatch) -> list:
    """What the route asked to be sent, without a notification catalogue.

    Patched on `app.api.opportunities` rather than on the dispatch module: the
    route imported the name, so rebinding it at the source would not be seen.
    """
    events: list = []

    async def _capture(event) -> int:
        events.append(event)
        return 0

    monkeypatch.setattr("app.api.opportunities.emit_and_enqueue", _capture)
    return events


async def test_two_recruiters_claiming_at_once_yields_one_winner() -> None:
    """A real race at 9pm, when the job orders arrive and everyone is looking."""
    tenant_id, first = await seed_tenant_with_user()
    second = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        results = await asyncio.gather(
            _claim(tenant_id, first, opportunity_id),
            _claim(tenant_id, second, opportunity_id),
        )
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409], [r.text for r in results]

        # And the winner is the one the row now names — a 200 that did not
        # actually take the job order would be worse than the 409.
        winner = next(r for r in results if r.status_code == 200)
        assert winner.json()["assigned_user_id"] == str(
            await _assigned_user_id(opportunity_id)
        )
    finally:
        await cleanup_tenant(tenant_id)


async def test_claiming_an_unassigned_job_order_succeeds() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        response = await _claim(tenant_id, user_id, opportunity_id)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": str(opportunity_id),
            "assigned_user_id": str(user_id),
        }
        assert await _assigned_user_id(opportunity_id) == user_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_claiming_an_already_assigned_job_order_is_a_conflict() -> None:
    """Not a 403: the job order is not forbidden, it is taken."""
    tenant_id, first = await seed_tenant_with_user()
    second = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=second)
    try:
        # The claimant is the tenant OWNER, so the row is visible to them and
        # the refusal cannot be mistaken for the visibility guard's 404.
        async with AdminSessionLocal() as s:
            await s.execute(
                User.__table__.update()
                .where(User.__table__.c.id == first)
                .values(role="owner")
            )
            await s.commit()

        response = await _claim(tenant_id, first, opportunity_id)
        assert response.status_code == 409, response.text
        assert await _assigned_user_id(opportunity_id) == second
    finally:
        await cleanup_tenant(tenant_id)


async def test_claiming_a_job_order_you_cannot_see_is_a_404() -> None:
    """404, never 409 — a 409 would confirm the id exists."""
    tenant_id, user_id = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=other)
    try:
        response = await _claim(tenant_id, user_id, opportunity_id)
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "No such job order."
    finally:
        await cleanup_tenant(tenant_id)


async def test_claiming_emits_nothing(captured_events) -> None:
    """You did it, you know."""
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        response = await _claim(tenant_id, user_id, opportunity_id)
        assert response.status_code == 200, response.text
        assert captured_events == []
    finally:
        await cleanup_tenant(tenant_id)


async def test_assigning_to_a_colleague_names_only_them(captured_events) -> None:
    tenant_id, assignee = await seed_tenant_with_user()
    colleague = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=assignee)
    try:
        async with _signed_in_client(assignee, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign",
                json={"user_id": str(colleague)},
            )
        assert response.status_code == 200, response.text
        assert response.json()["assigned_user_id"] == str(colleague)
        assert await _assigned_user_id(opportunity_id) == colleague

        assert len(captured_events) == 1, captured_events
        event = captured_events[0]
        assert event.kind == EVENT_OPPORTUNITY_ASSIGNED
        # Only them: `None` here would mean the whole agency.
        assert event.recipient_user_ids == (colleague,)
    finally:
        await cleanup_tenant(tenant_id)


async def test_releasing_a_job_order_puts_it_back_on_the_queue(
    captured_events,
) -> None:
    """`user_id: null` is queue work again, and nobody would otherwise learn
    it is available — so the fan-out is tenant-wide (`recipient_user_ids` is
    `None`, which means everybody; an empty tuple would mean nobody)."""
    tenant_id, assignee = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=assignee)
    try:
        async with _signed_in_client(assignee, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign", json={"user_id": None}
            )
        assert response.status_code == 200, response.text
        assert response.json()["assigned_user_id"] is None
        assert await _assigned_user_id(opportunity_id) is None

        assert len(captured_events) == 1, captured_events
        event = captured_events[0]
        assert event.kind == EVENT_OPPORTUNITY_NEW
        assert event.recipient_user_ids is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_non_owner_cannot_hand_on_someone_elses_job_order() -> None:
    """A share is sight, not the right to reassign."""
    from app.models.opportunity_share import OpportunityShare

    tenant_id, assignee = await seed_tenant_with_user()
    colleague = await _second_user(tenant_id)
    third = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=assignee)
    try:
        async with AdminSessionLocal() as s:
            s.add(
                OpportunityShare(
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=colleague,
                )
            )
            await s.commit()

        async with _signed_in_client(colleague, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign",
                json={"user_id": str(third)},
            )
        assert response.status_code == 403, response.text
        assert await _assigned_user_id(opportunity_id) == assignee
    finally:
        await cleanup_tenant(tenant_id)


async def test_assigning_an_unassigned_job_order_is_refused_for_a_recruiter() -> None:
    """`can_edit` deliberately refuses an unassigned row: claiming it, not
    assigning it, is how an ordinary recruiter takes it on."""
    tenant_id, user_id = await seed_tenant_with_user()
    colleague = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign",
                json={"user_id": str(colleague)},
            )
        assert response.status_code == 403, response.text
    finally:
        await cleanup_tenant(tenant_id)


async def test_the_owner_may_assign_anyone(captured_events) -> None:
    tenant_id, boss = await seed_tenant_with_user(role="owner")
    recruiter = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        async with _signed_in_client(boss, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign",
                json={"user_id": str(recruiter)},
            )
        assert response.status_code == 200, response.text
        assert await _assigned_user_id(opportunity_id) == recruiter
        assert captured_events[0].recipient_user_ids == (recruiter,)
    finally:
        await cleanup_tenant(tenant_id)


async def test_assigning_to_a_stranger_is_refused() -> None:
    """The target must be a colleague. The composite FK would refuse it too,
    but as a 500 rather than an answer."""
    tenant_id, assignee = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=assignee)
    try:
        async with _signed_in_client(assignee, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign",
                json={"user_id": str(uuid.uuid4())},
            )
        assert response.status_code == 422, response.text
        assert await _assigned_user_id(opportunity_id) == assignee
    finally:
        await cleanup_tenant(tenant_id)


async def test_assigning_to_yourself_says_nothing(captured_events) -> None:
    """Same reasoning as claiming: you did it, you know."""
    tenant_id, assignee = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=assignee)
    try:
        async with _signed_in_client(assignee, tenant_id) as c:
            response = await c.post(
                f"/api/opportunities/{opportunity_id}/assign",
                json={"user_id": str(assignee)},
            )
        assert response.status_code == 200, response.text
        assert captured_events == []
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_belongs_to_whoever_typed_it() -> None:
    """Not the client's assignee — the person at the keyboard."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={
                    "company_name_raw": "Acme Pte Ltd",
                    "job_title_raw": "Java Developer",
                    "location_raw": "Jurong",
                    "salary_raw": "6k neg.",
                },
            )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["source"] == "manual"
        assert body["assigned_user_id"] == str(user_id)
        assert body["client_id"] is None

        opportunity_id = uuid.UUID(body["id"])
        assert await _assigned_user_id(opportunity_id) == user_id
        async with AdminSessionLocal() as s:
            row = (
                await s.execute(
                    Opportunity.__table__.select().where(
                        Opportunity.__table__.c.id == opportunity_id
                    )
                )
            ).one()
        assert row.source == Opportunity.MANUAL
        assert row.email_message_id is None
        assert row.company_name_raw == "Acme Pte Ltd"
        # Dated so it sorts beside the extracted rows rather than sinking.
        assert row.received_datetime is not None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_appears_in_the_list() -> None:
    """The point of typing one in: it joins the same list as the extracted ones."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            created = await c.post(
                "/api/opportunities", json={"company_name_raw": "Acme Pte Ltd"}
            )
            assert created.status_code == 201, created.text
            listed = (await c.get("/api/opportunities")).json()
        assert [item["id"] for item in listed["items"]] == [created.json()["id"]]
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_keeps_the_client_it_was_given() -> None:
    """`client_id` comes from the body and may be absent; when given it must
    survive, or reassigning a client could never find its job orders."""
    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with AdminSessionLocal() as s:
            s.add(
                Client(
                    id=client_id,
                    tenant_id=tenant_id,
                    name="Acme Pte Ltd",
                    name_normalized="acme pte ltd",
                )
            )
            await s.commit()

        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={"company_name_raw": "Acme Pte Ltd", "client_id": str(client_id)},
            )
        assert response.status_code == 201, response.text
        assert response.json()["client_id"] == str(client_id)
    finally:
        await cleanup_tenant(tenant_id)
