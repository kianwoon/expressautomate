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
import contextlib
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
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user


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


def _claim_landing_before_the_write(monkeypatch, opportunity_id, winner) -> None:
    """Hand the job order to `winner` after the request starts, before the
    route's compare-and-set runs.

    `asyncio.gather` above proves the mechanism but cannot name the loser, so
    for years the losing branch was only sometimes executed — and when another
    file ran first it produced a 404 nobody had asked for. Wrapping the
    route's own `tenant_session` puts the competing claim in exactly one
    place: the session is open, so the loser's request is genuinely underway,
    and the UPDATE that follows genuinely finds the row already taken.
    """
    from app.api import opportunities as module

    original = module.tenant_session

    @contextlib.asynccontextmanager
    async def _session(tenant_id):
        async with original(tenant_id) as session:
            async with AdminSessionLocal() as s:
                await s.execute(
                    Opportunity.__table__.update()
                    .where(Opportunity.__table__.c.id == opportunity_id)
                    .values(assigned_user_id=winner)
                )
                await s.commit()
            yield session

    monkeypatch.setattr(module, "tenant_session", _session)


async def test_losing_the_race_says_someone_took_it_not_that_it_vanished(
    monkeypatch,
) -> None:
    """The loser must be told the truth.

    They saw the job order on the queue a second ago; it is real, and it is
    gone. A 404 reads as a bug in the list they were just looking at.
    """
    tenant_id, loser = await seed_tenant_with_user()
    winner = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        _claim_landing_before_the_write(monkeypatch, opportunity_id, winner)

        response = await _claim(tenant_id, loser, opportunity_id)

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Someone else has taken this job order."
        # And the loser took nothing on the way past.
        assert await _assigned_user_id(opportunity_id) == winner
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_id_that_names_nothing_is_still_404() -> None:
    """The other half of the discrimination: 409 must not become the answer
    to every failed claim, or it turns into an oracle for ids that do not
    exist."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        response = await _claim(tenant_id, user_id, uuid.uuid4())
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "No such job order."
    finally:
        await cleanup_tenant(tenant_id)


async def test_another_agencys_taken_job_order_is_404_not_409() -> None:
    """RLS, not the new branch, is what keeps agencies apart — assert it,
    because the 409 branch reads the row without the visibility guard."""
    tenant_id, user_id = await seed_tenant_with_user()
    other_tenant_id, other_user = await seed_tenant_with_user()
    try:
        opportunity_id = await _opportunity(
            other_tenant_id, assigned_user_id=other_user
        )
        response = await _claim(tenant_id, user_id, opportunity_id)
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "No such job order."
    finally:
        await cleanup_tenant(tenant_id)
        await cleanup_tenant(other_tenant_id)


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


async def test_claiming_a_colleagues_job_order_is_409_not_404() -> None:
    """This test used to assert 404, and the reason it no longer can is worth
    writing down.

    The old rule was "a 409 would confirm the id exists", so a recruiter who
    asked to claim a job order held by a colleague was told there was no such
    job order. But *the loser of a race is in exactly that state* — by the
    time they ask, the winner holds the row — and no query can tell the two
    apart, because they are the same row in the same condition. Keeping the
    old rule meant lying to every recruiter who lost a race, which is the
    common case; changing it discloses one bit, to a signed-in colleague of
    the same agency, about a job order id they were already holding.

    The boundary that matters is unchanged and is asserted next door:
    `test_another_agencys_taken_job_order_is_404_not_409`. RLS, not this
    branch, keeps agencies apart.
    """
    tenant_id, user_id = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=other)
    try:
        response = await _claim(tenant_id, user_id, opportunity_id)
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Someone else has taken this job order."
        assert await _assigned_user_id(opportunity_id) == other
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
        # No client_id was sent, but a company name was typed, so the route
        # resolves it to a (newly created) client rather than leaving it null.
        assert body["client_id"] is not None

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


async def test_a_hand_typed_job_order_creates_a_client_for_a_new_company() -> None:
    """No `client_id`, a company name nobody has seen before: a client is
    created and linked, in the same transaction as the opportunity."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={"company_name_raw": "Brand New Co Pte Ltd"},
            )
        assert response.status_code == 201, response.text
        client_id = response.json()["client_id"]
        assert client_id is not None

        async with AdminSessionLocal() as s:
            row = (
                await s.execute(
                    Client.__table__.select().where(Client.__table__.c.id == uuid.UUID(client_id))
                )
            ).one()
        assert row.tenant_id == tenant_id
        assert row.name == "Brand New Co Pte Ltd"
        assert row.name_normalized == "brand new"
        assert row.source == Client.MANUAL
        assert row.status == Client.CONFIRMED
        assert row.email_domain is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_links_an_existing_company_by_name() -> None:
    """A differing legal suffix still matches the normalised name, and no
    second client is created."""
    tenant_id, user_id = await seed_tenant_with_user()
    existing_id = uuid.uuid4()
    try:
        async with AdminSessionLocal() as s:
            s.add(
                Client(
                    id=existing_id,
                    tenant_id=tenant_id,
                    name="Acme",
                    name_normalized="acme",
                )
            )
            await s.commit()

        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={"company_name_raw": "Acme Pte Ltd"},
            )
        assert response.status_code == 201, response.text
        assert response.json()["client_id"] == str(existing_id)

        async with AdminSessionLocal() as s:
            rows = (
                await s.execute(
                    Client.__table__.select().where(
                        Client.__table__.c.tenant_id == tenant_id
                    )
                )
            ).all()
        assert len(rows) == 1
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_with_blank_company_name_creates_no_client() -> None:
    """Whitespace-only is the same as absent — nothing to resolve, so nothing
    is created."""
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={"company_name_raw": "   "},
            )
        assert response.status_code == 201, response.text
        assert response.json()["client_id"] is None

        async with AdminSessionLocal() as s:
            rows = (
                await s.execute(
                    Client.__table__.select().where(
                        Client.__table__.c.tenant_id == tenant_id
                    )
                )
            ).all()
        assert rows == []
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_does_not_match_a_merged_client() -> None:
    """A merged row's identity belongs to its target; matching it would
    silently resurrect an identity a recruiter deliberately retired."""
    tenant_id, user_id = await seed_tenant_with_user()
    merged_id = uuid.uuid4()
    survivor_id = uuid.uuid4()
    try:
        async with AdminSessionLocal() as s:
            s.add(
                Client(
                    id=survivor_id,
                    tenant_id=tenant_id,
                    name="Acme Holdings",
                    name_normalized="acme holdings",
                )
            )
            s.add(
                Client(
                    id=merged_id,
                    tenant_id=tenant_id,
                    name="Acme",
                    name_normalized="acme",
                    status=Client.MERGED,
                    merged_into_client_id=survivor_id,
                )
            )
            await s.commit()

        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={"company_name_raw": "Acme Pte Ltd"},
            )
        assert response.status_code == 201, response.text
        client_id = response.json()["client_id"]
        assert client_id is not None
        assert client_id != str(merged_id)
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_refuses_a_client_that_does_not_exist() -> None:
    """A picked client that was deleted or merged before Save is pressed.

    The composite foreign key would refuse the insert either way; what this
    pins is that the recruiter is told which field is wrong, in the same
    sentence the link route uses, instead of being shown a 500.
    """
    tenant_id, user_id = await seed_tenant_with_user()
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={
                    "company_name_raw": "Acme Pte Ltd",
                    "client_id": str(uuid.uuid4()),
                },
            )
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "That client is not in this agency."
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_hand_typed_job_order_refuses_another_agencys_client() -> None:
    """RLS makes the neighbour's client simply not found, so it reads as the
    same refusal — and never as a 500 that would confirm the id exists."""
    tenant_id, user_id = await seed_tenant_with_user()
    other_tenant_id, _other_user = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with AdminSessionLocal() as s:
            s.add(
                Client(
                    id=client_id,
                    tenant_id=other_tenant_id,
                    name="Neighbour Pte Ltd",
                    name_normalized="neighbour pte ltd",
                )
            )
            await s.commit()

        async with _signed_in_client(user_id, tenant_id) as c:
            response = await c.post(
                "/api/opportunities",
                json={
                    "company_name_raw": "Acme Pte Ltd",
                    "client_id": str(client_id),
                },
            )
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "That client is not in this agency."
    finally:
        await cleanup_tenant(tenant_id)
        await cleanup_tenant(other_tenant_id)


async def test_a_client_deleted_mid_request_is_still_a_refusal(monkeypatch) -> None:
    """The race the pre-check cannot win.

    A pre-check and the write that follows it are two statements, so a client
    deleted in between still reaches the database as a foreign key violation.
    Neutering the pre-check is how that window is made reliably reproducible:
    what is left is exactly the state of the world where the delete landed
    after the check, and the caller must still be told the client is gone
    rather than shown a 500.
    """
    from types import SimpleNamespace

    from app.api import opportunities as routes

    async def _still_there(_session, client_id):
        """What the pre-check saw a moment before the delete landed."""
        return SimpleNamespace(id=client_id, name="Acme Pte Ltd", assigned_user_id=None)

    monkeypatch.setattr(routes, "_load_client_in_agency", _still_there)

    tenant_id, user_id = await seed_tenant_with_user()
    try:
        async with _signed_in_client(user_id, tenant_id) as c:
            created = await c.post(
                "/api/opportunities",
                json={
                    "company_name_raw": "Acme Pte Ltd",
                    "client_id": str(uuid.uuid4()),
                },
            )
            assert created.status_code == 422, created.text
            assert created.json()["detail"] == "That client is not in this agency."

            opportunity_id = await _opportunity(tenant_id, assigned_user_id=user_id)
            linked = await c.post(
                f"/api/opportunities/{opportunity_id}/client",
                json={"client_id": str(uuid.uuid4())},
            )
        assert linked.status_code == 422, linked.text
        assert linked.json()["detail"] == "That client is not in this agency."
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_unrelated_violation_is_not_blamed_on_the_client() -> None:
    """A refusal must not name a cause that had nothing to do with it.

    The create route can fail its INSERT for reasons that have no client in
    them at all — a check constraint, or the creating recruiter's account
    being offboarded between sign-in and the write, which takes out the
    composite assignee foreign key. Telling that recruiter their client is not
    in the agency is worse than a 500: a 500 says something unexpected
    happened, while this asserts a specific falsehood about their data, and
    the request here does not carry a `client_id` at all.

    `salary_period` is the violation used because it is the cheapest one to
    provoke and the guard cannot tell the difference: any `IntegrityError` off
    that flush is what is under test. The route never sets the column, so it
    is put on the row by a `before_insert` listener — what matters is that a
    non-client constraint fires on the same statement.
    """
    from sqlalchemy import event

    def _break_the_row(_mapper, _connection, target) -> None:
        target.salary_period = "Month"  # not in the constraint's vocabulary

    tenant_id, user_id = await seed_tenant_with_user()
    event.listen(Opportunity, "before_insert", _break_the_row)
    # `raise_app_exceptions=False` so the unhandled error becomes the 500 a
    # browser would see, rather than being re-raised into the test.
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        follow_redirects=False,
    )
    http.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )
    try:
        async with http as c:
            created = await c.post(
                "/api/opportunities",
                json={"company_name_raw": "Acme Pte Ltd"},
            )
        assert created.status_code == 500, created.status_code
        assert "not in this agency" not in created.text
    finally:
        event.remove(Opportunity, "before_insert", _break_the_row)
        await cleanup_tenant(tenant_id)


async def test_a_real_client_does_not_absorb_an_unrelated_violation() -> None:
    """The same falsehood, through the door the guard is actually open for.

    The sibling test above sends no `client_id`, so the route never enters the
    guard. This one does send one — a real client, in this tenant — and still
    breaks the row on a constraint that has nothing to do with clients. The
    recruiter must be told something unexpected happened, not handed a
    sentence about their client that is simply untrue.
    """
    from sqlalchemy import event

    def _break_the_row(_mapper, _connection, target) -> None:
        target.salary_period = "Month"  # not in the constraint's vocabulary

    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
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

    event.listen(Opportunity, "before_insert", _break_the_row)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        follow_redirects=False,
    )
    http.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )
    try:
        async with http as c:
            created = await c.post(
                "/api/opportunities",
                json={
                    "company_name_raw": "Acme Pte Ltd",
                    "client_id": str(client_id),
                },
            )
        assert created.status_code == 500, created.status_code
        assert "not in this agency" not in created.text
    finally:
        event.remove(Opportunity, "before_insert", _break_the_row)
        await cleanup_tenant(tenant_id)
