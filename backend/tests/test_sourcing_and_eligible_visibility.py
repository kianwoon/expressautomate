# allow-hardcode: the names, emails and SQL below are test fixture content,
# not an oracle and not configuration.
"""The two routes that read a colleague's job order and used not to notice.

`sourcing.py` and `candidates.py` both fetched an `Opportunity` by id under
RLS alone. RLS draws the line between agencies and nothing else, so a
recruiter holding an id — from a share since withdrawn, say — reached another
recruiter's shortlist: candidate names and scores, the job order's
`placement_type`, and, on the POST, that recruiter's daily run quota.

Every test here comes in a pair, and the pairs are the point. A guard that
refuses everybody passes every negative test in this file; only the positive
ones say the feature still works. So each route is asserted twice — refused
for a stranger, allowed for the assignee — and once more for a share
recipient, because sharing is the mechanism by which a second recruiter is
*meant* to get in.

Redis is never touched: `enqueue` is replaced at the point `app.api.sourcing`
imported it.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import sourcing
from app.main import app
from app.models.opportunity_share import OpportunityShare
from app.models.sourcing import SourcingRun
from app.models.tenant import User
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


@pytest.fixture
def queued(monkeypatch):
    """Every job the route tried to enqueue. Redis is never reached."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(sourcing, "enqueue", _enqueue)
    return jobs


@pytest.fixture
async def agency():
    """One agency, two ordinary recruiters, and a job order belonging to one.

    Both users are `recruiter`, not `owner`: an owner sees the whole pipeline
    by design, so an owner fixture would pass every assertion below without
    the guard existing at all — which is how these two leaks got shipped.

    The job order's email hangs off *Bea's* mailbox. The predicate lets the
    recipient of the original mail keep sight of what was extracted from it,
    so leaving it on Ann's mailbox would make the row visible for a reason
    that has nothing to do with assignment.
    """
    tenant_id = uuid.uuid4()
    ann, bea = uuid.uuid4(), uuid.uuid4()  # ann: the stranger. bea: the assignee.
    mailbox_id, message_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tenant_id, "n": f"agency-{tenant_id.hex[:6]}"},
        )
        # The ORM for users: `role` is NOT NULL with a Python-side default,
        # which a hand-written INSERT never fires.
        s.add(User(id=ann, tenant_id=tenant_id, email=f"ann-{ann.hex[:6]}@agency.sg"))
        s.add(User(id=bea, tenant_id=tenant_id, email=f"bea-{bea.hex[:6]}@agency.sg"))
        await s.flush()
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, retention_months) VALUES (:i, :t, :u, :m, 'inbox', 'user', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id, "u": bea, "m": f"oid-{mailbox_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " internet_message_id, received_datetime)"
                " VALUES (:i, :t, :m, :g, :n, now())"
            ),
            {
                "i": message_id,
                "t": tenant_id,
                "m": mailbox_id,
                "g": f"graph-{message_id.hex}",
                "n": f"<{message_id.hex}@example.sg>",
            },
        )
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " assigned_user_id, source, placement_type, review_status, quality_state)"
                " VALUES (:i, :t, :m, :u, 'pipeline', 'local_hire', 'needs_review', 'likely')"
            ),
            {"i": opportunity_id, "t": tenant_id, "m": message_id, "u": bea},
        )
        await s.commit()

    yield tenant_id, ann, bea, opportunity_id

    async with AdminSessionLocal() as s:
        for table in (
            "sourcing_matches",
            "sourcing_runs",
            "opportunity_shares",
            "candidate_submissions",
            "client_mentions",
            "clients",
            "candidates",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()
    await cleanup_tenant(tenant_id)


def _http(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, user_id, tenant_id)
    return client


async def _share_with(tenant_id, opportunity_id, user_id, by_user_id) -> None:
    async with AdminSessionLocal() as s:
        s.add(
            OpportunityShare(
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                scope=OpportunityShare.SCOPE_USER,
                shared_with_user_id=user_id,
                shared_by_user_id=by_user_id,
            )
        )
        await s.commit()


async def _a_run(tenant_id, opportunity_id, created_by) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            SourcingRun(
                id=run_id,
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                state=SourcingRun.PENDING,
                created_by=created_by,
            )
        )
        await s.commit()
    return run_id


async def _a_candidate(tenant_id) -> uuid.UUID:
    candidate_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name)"
                " VALUES (:i, :t, 'Siti Rahman')"
            ),
            {"i": candidate_id, "t": tenant_id},
        )
        await s.commit()
    return candidate_id


# --- refused ---------------------------------------------------------------


async def test_starting_a_shortlist_on_a_colleagues_job_order_is_a_404(
    agency, queued
) -> None:
    """404, never 403 — a 403 would confirm the id exists. And nothing is
    queued: the quota this route spends belongs to the other recruiter."""
    tenant_id, ann, _bea, opportunity_id = agency
    async with _http(tenant_id, ann) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."
    assert queued == []

    async with AdminSessionLocal() as s:
        started = (
            await s.execute(
                text("SELECT count(*) FROM sourcing_runs WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert started == 0


async def test_reading_the_latest_shortlist_of_a_colleagues_job_order_is_a_404(
    agency,
) -> None:
    tenant_id, ann, bea, opportunity_id = agency
    await _a_run(tenant_id, opportunity_id, bea)

    async with _http(tenant_id, ann) as http:
        response = await http.get(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."


async def test_reading_one_run_of_a_colleagues_job_order_is_a_404(agency) -> None:
    """The job order is refused before the run is looked up, so a real run id
    is no more use than a made-up one."""
    tenant_id, ann, bea, opportunity_id = agency
    run_id = await _a_run(tenant_id, opportunity_id, bea)

    async with _http(tenant_id, ann) as http:
        response = await http.get(
            f"/api/opportunities/{opportunity_id}/sourcing/{run_id}"
        )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."


async def test_filtering_candidates_by_a_colleagues_job_order_is_a_404(agency) -> None:
    """`?eligible_for=` leaked two things: that the job order exists, and —
    via the 409 branch — whether its `placement_type` had been set."""
    tenant_id, ann, _bea, opportunity_id = agency
    await _a_candidate(tenant_id)

    async with _http(tenant_id, ann) as http:
        response = await http.get(f"/api/candidates?eligible_for={opportunity_id}")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."


# --- allowed ---------------------------------------------------------------


async def test_the_assignee_can_start_a_shortlist(agency, queued) -> None:
    """The positive half of the pair. A guard that refused everybody would
    pass every test above and fail this one."""
    tenant_id, _ann, bea, opportunity_id = agency
    async with _http(tenant_id, bea) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 202, response.text
    assert response.json()["opportunity_id"] == str(opportunity_id)
    assert [name for name, _ in queued] == ["run_sourcing"]


async def test_the_assignee_can_read_their_own_shortlist(agency) -> None:
    tenant_id, _ann, bea, opportunity_id = agency
    run_id = await _a_run(tenant_id, opportunity_id, bea)

    async with _http(tenant_id, bea) as http:
        latest = await http.get(f"/api/opportunities/{opportunity_id}/sourcing")
        one = await http.get(f"/api/opportunities/{opportunity_id}/sourcing/{run_id}")

    assert latest.status_code == 200, latest.text
    assert latest.json()["run"]["id"] == str(run_id)
    assert one.status_code == 200, one.text
    assert one.json()["run"]["id"] == str(run_id)


async def test_the_assignee_can_filter_candidates_by_their_own_job_order(agency) -> None:
    tenant_id, _ann, bea, opportunity_id = agency
    await _a_candidate(tenant_id)

    async with _http(tenant_id, bea) as http:
        response = await http.get(f"/api/candidates?eligible_for={opportunity_id}")

    assert response.status_code == 200, response.text
    assert response.json()["excluded_ineligible"] is not None


async def test_a_share_recipient_reaches_the_shortlist_and_the_filter(agency) -> None:
    """Sharing is the mechanism by which a second recruiter is *meant* to get
    in, so the guard has to let it through — on both routes, and on the POST
    too: being shown a job order is being allowed to work it, which is not
    the same as being allowed to edit it (`test_opportunity_visibility_routes`
    covers that line)."""
    tenant_id, ann, bea, opportunity_id = agency
    await _share_with(tenant_id, opportunity_id, ann, bea)
    run_id = await _a_run(tenant_id, opportunity_id, bea)
    await _a_candidate(tenant_id)

    async with _http(tenant_id, ann) as http:
        latest = await http.get(f"/api/opportunities/{opportunity_id}/sourcing")
        one = await http.get(f"/api/opportunities/{opportunity_id}/sourcing/{run_id}")
        eligible = await http.get(f"/api/candidates?eligible_for={opportunity_id}")

    assert latest.status_code == 200, latest.text
    assert latest.json()["run"]["id"] == str(run_id)
    assert one.status_code == 200, one.text
    assert eligible.status_code == 200, eligible.text


async def test_a_withdrawn_share_closes_the_door_again(agency) -> None:
    """The scenario the finding was written about: an id kept from a share
    that has since been revoked. The route must stop answering."""
    tenant_id, ann, bea, opportunity_id = agency
    await _share_with(tenant_id, opportunity_id, ann, bea)
    run_id = await _a_run(tenant_id, opportunity_id, bea)

    async with _http(tenant_id, ann) as http:
        assert (
            await http.get(f"/api/opportunities/{opportunity_id}/sourcing/{run_id}")
        ).status_code == 200

        async with AdminSessionLocal() as s:
            await s.execute(
                text("DELETE FROM opportunity_shares WHERE opportunity_id = :i"),
                {"i": opportunity_id},
            )
            await s.commit()

        after = await http.get(f"/api/opportunities/{opportunity_id}/sourcing/{run_id}")

    assert after.status_code == 404, after.text
    assert after.json()["detail"] == "No such job order."
