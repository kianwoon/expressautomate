# allow-hardcode: the names, titles and SQL below are test fixture content,
# not an oracle and not configuration.
"""Starting a shortlist, reading one back, and recording a submission.

The questions worth asking here are the adversarial ones. Can a caller reach
another agency's job order or another agency's run. Does the quota refuse
before a row exists, or after. And — the hole this task was written to close —
does a run whose client could not be resolved *say so*, rather than quietly
producing a shortlist with the already-submitted exclusion switched off.

Nothing here reaches a model or Redis: `enqueue` is replaced at the point
`app.api.sourcing` imported it.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import sourcing
from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models.sourcing import SourcingRun
from app.services.sourcing.client_resolution import resolve_client
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


async def _seed_agency() -> tuple[uuid.UUID, uuid.UUID]:
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    return tid, uid


async def _drop_agency(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in (
            "sourcing_matches",
            "sourcing_runs",
            "candidate_submissions",
            "client_mentions",
            "clients",
            "candidates",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.commit()
    await cleanup_tenant(tid)


@pytest.fixture
async def agency():
    tid, uid = await _seed_agency()
    yield tid, uid
    await _drop_agency(tid)


@pytest.fixture
async def other_agency():
    """A second agency, so "not yours" can be told apart from "not there"."""
    tid, uid = await _seed_agency()
    yield tid, uid
    await _drop_agency(tid)


@pytest.fixture
def queued(monkeypatch):
    """Every job the route tried to enqueue. Redis is never touched."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(sourcing, "enqueue", _enqueue)
    return jobs


@pytest.fixture
def enqueue_fails(monkeypatch):
    """Redis down: `enqueue` fails soft, exactly as `queue.py` does."""

    async def _enqueue(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(sourcing, "enqueue", _enqueue)


def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, uid, tid)
    return client


async def _opportunity(tenant_id: uuid.UUID, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A job order and the email it arrived on — the client link is the email,
    not a column, which is the whole subject of this module."""
    mailbox_id, message_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, retention_months) VALUES (:i, :t, :u, :m, 'inbox', 'user', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id, "u": user_id, "m": f"oid-{mailbox_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " internet_message_id, subject)"
                " VALUES (:i, :t, :m, :g, :g, 'Nurse wanted')"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id, "g": message_id.hex},
        )
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_normalized, company_name_normalized, review_status,"
                " quality_state)"
                " VALUES (:i, :t, :e, 'staff nurse', 'acme health', 'ready', 'likely')"
            ),
            {"i": opportunity_id, "t": tenant_id, "e": message_id},
        )
        await s.commit()
    return opportunity_id, message_id


async def _client(tenant_id: uuid.UUID, name: str, domain: str | None = None) -> uuid.UUID:
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, email_domain,"
                " status) VALUES (:i, :t, :n, :n, :d, 'confirmed')"
            ),
            {"i": cid, "t": tenant_id, "n": name, "d": domain},
        )
        await s.commit()
    return cid


async def _mention(
    tenant_id: uuid.UUID, client_id: uuid.UUID, message_id: uuid.UUID, matched_by: str
) -> None:
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id,"
                " matched_by) VALUES (:i, :t, :c, :m, :b)"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "c": client_id, "m": message_id, "b": matched_by},
        )
        await s.commit()


async def _candidate(tenant_id: uuid.UUID, name: str = "Jane Tan") -> uuid.UUID:
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, record_status,"
                " pipeline_stage) VALUES (:i, :t, :n, 'active', 'new')"
            ),
            {"i": cid, "t": tenant_id, "n": name},
        )
        await s.commit()
    return cid


async def _run_row(run_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT state, client_id, client_unresolved_reason, failure_reason"
                    " FROM sourcing_runs WHERE id = :i"
                ),
                {"i": run_id},
            )
        ).one()


async def _count_runs(tenant_id: uuid.UUID) -> int:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM sourcing_runs WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()


# --- starting a run -------------------------------------------------------


async def test_starting_a_shortlist_answers_202_and_queues_the_job(agency, queued) -> None:
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(tenant_id, user_id) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == SourcingRun.PENDING
    assert queued[0][0] == "run_sourcing"
    assert queued[0][1]["run_id"] == body["id"]


async def test_another_agencys_job_order_is_not_found_not_forbidden(
    agency, other_agency, queued
) -> None:
    """404, never 403 — a 403 would confirm the id exists somewhere."""
    tenant_id, user_id = agency
    other_tenant, other_user = other_agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(other_tenant, other_user) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 404
    assert await _count_runs(tenant_id) == 0
    assert queued == []


async def test_past_the_quota_nothing_is_created(agency, queued, monkeypatch) -> None:
    """Refused in the route, so no `pending` row is stranded for a worker that
    will never claim it."""
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)
    monkeypatch.setattr(settings, "SOURCING_DAILY_RUN_QUOTA", 1)

    async with _http(tenant_id, user_id) as http:
        first = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")
        second = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert first.status_code == 202
    assert second.status_code == 429
    assert await _count_runs(tenant_id) == 1
    assert len(queued) == 1


async def test_a_run_that_could_not_be_queued_says_so_and_is_retryable(
    agency, enqueue_fails
) -> None:
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(tenant_id, user_id) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == SourcingRun.FAILED
    assert "Try again" in body["failure_reason"]
    row = await _run_row(uuid.UUID(body["id"]))
    assert row.state == SourcingRun.FAILED


# --- the client the exclusion was applied against -------------------------


async def test_a_run_whose_client_resolved_records_that_client(agency, queued) -> None:
    """Directive 1: nothing used to record which client the exclusion used."""
    tenant_id, user_id = agency
    opportunity_id, message_id = await _opportunity(tenant_id, user_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")
    await _mention(tenant_id, client_id, message_id, "email_domain")

    async with _http(tenant_id, user_id) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    body = response.json()
    assert body["client_id"] == str(client_id)
    assert body["client_unresolved_reason"] is None
    assert queued[0][1]["client_id"] == str(client_id)
    row = await _run_row(uuid.UUID(body["id"]))
    assert row.client_id == client_id


async def test_an_unresolvable_client_still_runs_but_says_the_check_did_not(
    agency, queued
) -> None:
    """Directive 2. Refusing would kill the feature for every unmatched
    client; silence is the re-pitching embarrassment."""
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(tenant_id, user_id) as http:
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 202
    body = response.json()
    assert body["client_id"] is None
    assert "did not run" in body["client_unresolved_reason"]
    assert queued[0][1]["client_id"] is None


async def test_a_domain_match_beats_a_name_match(agency) -> None:
    """Directive 3. A name match is a resemblance; the domain is a fact about
    where the mail came from."""
    tenant_id, user_id = agency
    opportunity_id, message_id = await _opportunity(tenant_id, user_id)
    named = await _client(tenant_id, "Acme Holdings")
    by_domain = await _client(tenant_id, "Acme Health", "acme.sg")
    await _mention(tenant_id, named, message_id, "name")
    await _mention(tenant_id, by_domain, message_id, "email_domain")

    async with tenant_session(tenant_id) as session:
        resolution = await resolve_client(
            session, tenant_id=tenant_id, opportunity_id=opportunity_id
        )

    assert resolution.client_id == by_domain
    assert resolution.reason is None


async def test_two_clients_named_on_one_email_is_treated_as_unresolved(agency) -> None:
    """Picking either would wrongly *exclude* a candidate never submitted
    anywhere near this job order — a worse error than skipping the check."""
    tenant_id, user_id = agency
    opportunity_id, message_id = await _opportunity(tenant_id, user_id)
    first = await _client(tenant_id, "Acme Holdings")
    second = await _client(tenant_id, "Acme Logistics")
    await _mention(tenant_id, first, message_id, "name")
    await _mention(tenant_id, second, message_id, "name")

    async with tenant_session(tenant_id) as session:
        resolution = await resolve_client(
            session, tenant_id=tenant_id, opportunity_id=opportunity_id
        )

    assert resolution.client_id is None
    assert "more than one client" in resolution.reason


# --- reading a run back ---------------------------------------------------


async def test_the_latest_run_reads_back_with_matches_best_first(agency, queued) -> None:
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)
    low = await _candidate(tenant_id, "Bob Lee")
    high = await _candidate(tenant_id, "Ann Ng")

    async with _http(tenant_id, user_id) as http:
        run_id = uuid.UUID(
            (await http.post(f"/api/opportunities/{opportunity_id}/sourcing")).json()["id"]
        )
        async with AdminSessionLocal() as s:
            for candidate_id, score in ((low, "0.1000"), (high, "0.9000")):
                await s.execute(
                    text(
                        "INSERT INTO sourcing_matches (id, tenant_id, run_id, candidate_id,"
                        " score, reasons) VALUES (:i, :t, :r, :c, :s, '[]'::jsonb)"
                    ),
                    {
                        "i": uuid.uuid4(),
                        "t": tenant_id,
                        "r": run_id,
                        "c": candidate_id,
                        "s": score,
                    },
                )
            await s.commit()

        body = (await http.get(f"/api/opportunities/{opportunity_id}/sourcing")).json()

    assert body["run"]["id"] == str(run_id)
    assert [m["candidate_id"] for m in body["matches"]] == [str(high), str(low)]
    # Four decimal places survive the trip out, as they do the trip in.
    assert body["matches"][0]["score"] == "0.9000"


async def test_a_job_order_with_no_run_is_not_a_404(agency) -> None:
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(tenant_id, user_id) as http:
        response = await http.get(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 200
    assert response.json() == {"run": None, "matches": []}


async def test_an_earlier_run_is_still_addressable(agency, queued) -> None:
    """"The list I sent on Tuesday" is the point of storing a run."""
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(tenant_id, user_id) as http:
        first = (await http.post(f"/api/opportunities/{opportunity_id}/sourcing")).json()["id"]
        second = (await http.post(f"/api/opportunities/{opportunity_id}/sourcing")).json()["id"]
        latest = (await http.get(f"/api/opportunities/{opportunity_id}/sourcing")).json()
        earlier = await http.get(f"/api/opportunities/{opportunity_id}/sourcing/{first}")

    assert latest["run"]["id"] == second
    assert earlier.status_code == 200
    assert earlier.json()["run"]["id"] == first


async def test_another_agency_cannot_read_a_run(agency, other_agency, queued) -> None:
    tenant_id, user_id = agency
    other_tenant, other_user = other_agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)

    async with _http(tenant_id, user_id) as http:
        run_id = (await http.post(f"/api/opportunities/{opportunity_id}/sourcing")).json()["id"]

    async with _http(other_tenant, other_user) as http:
        assert (
            await http.get(f"/api/opportunities/{opportunity_id}/sourcing/{run_id}")
        ).status_code == 404
        assert (
            await http.get(f"/api/opportunities/{opportunity_id}/sourcing")
        ).status_code == 404


# --- submissions ----------------------------------------------------------


async def test_a_submission_is_recorded_once_and_a_repeat_is_refused(agency) -> None:
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)
    candidate_id = await _candidate(tenant_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, user_id) as http:
        first = await http.post(
            f"/api/candidates/{candidate_id}/submissions",
            json={"client_id": str(client_id), "opportunity_id": str(opportunity_id)},
        )
        second = await http.post(
            f"/api/candidates/{candidate_id}/submissions",
            json={"client_id": str(client_id)},
        )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_another_agency_cannot_submit_your_candidate(agency, other_agency) -> None:
    tenant_id, user_id = agency
    other_tenant, other_user = other_agency
    candidate_id = await _candidate(tenant_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(other_tenant, other_user) as http:
        response = await http.post(
            f"/api/candidates/{candidate_id}/submissions",
            json={"client_id": str(client_id)},
        )

    assert response.status_code == 404


async def test_deleting_a_submission_restores_eligibility(agency) -> None:
    """The exclusion is the reason these routes exist; this is the round trip
    through `eligible_candidates` that proves it fires and stops firing."""
    from app.services.sourcing.eligible import eligible_candidates

    tenant_id, user_id = agency
    candidate_id = await _candidate(tenant_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, user_id) as http:
        created = await http.post(
            f"/api/candidates/{candidate_id}/submissions",
            json={"client_id": str(client_id)},
        )
        submission_id = created.json()["id"]

        async with tenant_session(tenant_id) as session:
            excluded = await eligible_candidates(
                session, tenant_id=tenant_id, client_id=client_id
            )
        assert candidate_id not in excluded

        removed = await http.delete(
            f"/api/candidates/{candidate_id}/submissions/{submission_id}"
        )

    assert removed.status_code == 200
    async with tenant_session(tenant_id) as session:
        restored = await eligible_candidates(session, tenant_id=tenant_id, client_id=client_id)
        # And with no client resolved at all, nobody is excluded — the honest
        # answer, and the one the run flags rather than hides.
        unfiltered = await eligible_candidates(session, tenant_id=tenant_id, client_id=None)
    assert candidate_id in restored
    assert candidate_id in unfiltered


async def test_submission_to_a_suspended_client_is_refused(agency) -> None:
    """Submitting is the outward-facing act, and the one that must not happen
    while a client is on hold."""
    tenant_id, user_id = agency
    opportunity_id, _message_id = await _opportunity(tenant_id, user_id)
    candidate_id = await _candidate(tenant_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, user_id) as http:
        await http.post(
            f"/api/clients/{client_id}/suspend", json={"reason": "Invoice 4021 unpaid"}
        )
        response = await http.post(
            f"/api/candidates/{candidate_id}/submissions",
            json={"client_id": str(client_id), "opportunity_id": str(opportunity_id)},
        )

    assert response.status_code == 409
    # The reason, not a generic failure — the recruiter must be able to act on it.
    assert "Invoice 4021 unpaid" in response.json()["detail"]


async def test_sourcing_still_runs_for_a_suspended_client(agency, queued) -> None:
    """Ranking is internal research. Blocking it would stop a recruiter
    preparing for the day the hold lifts."""
    tenant_id, user_id = agency
    opportunity_id, message_id = await _opportunity(tenant_id, user_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")
    await _mention(tenant_id, client_id, message_id, "email_domain")

    async with _http(tenant_id, user_id) as http:
        await http.post(f"/api/clients/{client_id}/suspend", json={"reason": "Invoice unpaid"})
        response = await http.post(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 202


async def test_another_agency_cannot_delete_a_submission(agency, other_agency) -> None:
    tenant_id, user_id = agency
    other_tenant, other_user = other_agency
    candidate_id = await _candidate(tenant_id)
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, user_id) as http:
        submission_id = (
            await http.post(
                f"/api/candidates/{candidate_id}/submissions",
                json={"client_id": str(client_id)},
            )
        ).json()["id"]

    async with _http(other_tenant, other_user) as http:
        response = await http.delete(
            f"/api/candidates/{candidate_id}/submissions/{submission_id}"
        )

    assert response.status_code == 404


async def _add_recruiter(tenant_id: uuid.UUID) -> uuid.UUID:
    """A second, non-owner user in the SAME tenant — the fixture's `agency`
    only ever seeds a `role='owner'` user, which can edit anything and so
    cannot exercise the visible-but-not-editable path."""
    uid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role)"
                " VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": uid, "t": tenant_id, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    return uid


async def test_withdraw_requires_edit_rights_not_just_visibility(agency) -> None:
    """`record_submission` stays on the visibility guard (a share recipient
    may put a candidate shown to them in front of a client), but withdrawing
    deletes a colleague's row, so it needs ownership. A recruiter who can only
    SEE the unowned candidate gets 403; the owner-role user, who can edit
    anything, gets 200."""
    tenant_id, owner_id = agency
    recruiter_id = await _add_recruiter(tenant_id)
    candidate_id = await _candidate(tenant_id)  # unowned: visible to all, editable by none
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, owner_id) as http:
        submission_id = (
            await http.post(
                f"/api/candidates/{candidate_id}/submissions",
                json={"client_id": str(client_id)},
            )
        ).json()["id"]

    async with _http(tenant_id, recruiter_id) as http:
        # Can see it (unowned candidates are tenant-wide visible)...
        seen = await http.get(f"/api/candidates/{candidate_id}")
        assert seen.status_code == 200
        # ...but cannot withdraw a submission on it: visibility, not edit rights.
        refused = await http.delete(
            f"/api/candidates/{candidate_id}/submissions/{submission_id}"
        )
        assert refused.status_code == 403, refused.text

    async with _http(tenant_id, owner_id) as http:
        removed = await http.delete(
            f"/api/candidates/{candidate_id}/submissions/{submission_id}"
        )
        assert removed.status_code == 200, removed.text


async def test_the_recorder_can_withdraw_their_own_submission_without_edit_rights(
    agency,
) -> None:
    """A share recipient may record a submission on a candidate merely visible
    to them (`record_submission` stays on the visibility guard). Without this
    allowance a misclick would be permanent for them, since they will never
    hold edit rights on someone else's candidate. Whoever created the row may
    always undo it."""
    tenant_id, owner_id = agency
    recruiter_id = await _add_recruiter(tenant_id)
    candidate_id = await _candidate(tenant_id)  # unowned: visible to all, editable by none
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, recruiter_id) as http:
        submission_id = (
            await http.post(
                f"/api/candidates/{candidate_id}/submissions",
                json={"client_id": str(client_id)},
            )
        ).json()["id"]

        removed = await http.delete(
            f"/api/candidates/{candidate_id}/submissions/{submission_id}"
        )
        assert removed.status_code == 200, removed.text


async def test_a_third_recruiter_who_neither_owns_nor_recorded_it_is_refused(
    agency,
) -> None:
    """Visibility plus authorship of the specific row are the only two ways
    in; a bystander who merely sees the candidate gets 403, same as before
    this allowance existed."""
    tenant_id, owner_id = agency
    recorder_id = await _add_recruiter(tenant_id)
    bystander_id = await _add_recruiter(tenant_id)
    candidate_id = await _candidate(tenant_id)  # unowned: visible to all, editable by none
    client_id = await _client(tenant_id, "Acme Health", "acme.sg")

    async with _http(tenant_id, recorder_id) as http:
        submission_id = (
            await http.post(
                f"/api/candidates/{candidate_id}/submissions",
                json={"client_id": str(client_id)},
            )
        ).json()["id"]

    async with _http(tenant_id, bystander_id) as http:
        refused = await http.delete(
            f"/api/candidates/{candidate_id}/submissions/{submission_id}"
        )
        assert refused.status_code == 403, refused.text


# --- routing --------------------------------------------------------------


def test_the_sourcing_paths_are_declared_and_under_api() -> None:
    """The include-order guard. A literal segment declared after a `{param}`
    route on the same prefix is never reached, and the failure looks like a
    malformed-UUID 422 rather than a routing bug."""
    paths = set(app.openapi()["paths"])
    for expected in (
        "/api/opportunities/{opportunity_id}/sourcing",
        "/api/opportunities/{opportunity_id}/sourcing/{run_id}",
        "/api/candidates/{candidate_id}/submissions",
        "/api/candidates/{candidate_id}/submissions/{submission_id}",
    ):
        assert expected in paths, f"{expected} missing — the route would be shadowed"
