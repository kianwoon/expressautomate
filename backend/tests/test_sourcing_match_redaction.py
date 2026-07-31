# allow-hardcode: the names, emails and SQL below are test fixture content,
# not an oracle and not configuration.
"""Sourcing reaches candidates by SET MEMBERSHIP, and that was its blind spot.

`test_candidate_routes_guarded.py` covers routes that read a candidate BY ID.
Sourcing never names one: `persist.py` and `eligible.py` score the whole
tenant, and the run is stored once. So a recruiter reading a shortlist was
handed `explanation`, `explanation_evidence` (verbatim CV quotes) and
`reasons` for candidates a colleague holds privately — far more than the 409
collision path ever discloses, where the same colleague learns only an
abbreviated, contact-masked name and who holds the person.

The rule implemented and asserted here: scoring stays agency-wide (an agency
that cannot shortlist across its own book has no reason to run sourcing), and
redaction happens per-viewer at READ time, to exactly the tier the 409
already defines. Not a new disclosure tier — one more surface for an existing
one.

Every test comes in a pair. A redaction that hid everything from everybody
would pass the negative half; the positive half — the same stored run, read
after the candidate is shared — is what says the feature still works.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from app.models.candidate_share import CandidateShare
from app.models.sourcing import SourcingMatch, SourcingRun
from app.models.tenant import User
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in

# The three things a private candidate must not disclose, and the literal
# strings asserted against the whole response body rather than against dict
# keys: a nested payload can carry them somewhere the assertion did not look.
EXPLANATION = "Ran the same MRT-line retail rollout for a rival agency"
EVIDENCE = "Led 14 outlets across the North-East line"
REASON = "eight years of the exact vertical"


@pytest.fixture
async def agency():
    """One agency, two ordinary recruiters, one job order, one candidate.

    Both `recruiter`, not `owner`: an owner sees the whole pipeline by design
    and would pass every assertion below without any redaction existing.

    Ann privately holds the candidate. Bea holds the job order, so Bea is the
    one entitled to read the run — the leak is not about reaching the run, it
    is about what the run says once reached.
    """
    tenant_id = uuid.uuid4()
    ann, bea = uuid.uuid4(), uuid.uuid4()
    mailbox_id, message_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidate_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tenant_id, "n": f"agency-{tenant_id.hex[:6]}"},
        )
        # The ORM for users: `role` is NOT NULL with a Python-side default,
        # which a hand-written INSERT never fires. `preferred_name` is what
        # the 409 body names the holder by, so it is set here to prove the
        # redacted shape names her the same way.
        s.add(
            User(
                id=ann,
                tenant_id=tenant_id,
                email=f"ann-{ann.hex[:6]}@agency.sg",
                preferred_name="Ann Lim",
            )
        )
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
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, owner_id)"
                " VALUES (:i, :t, 'Siti Nur Rahman', :o)"
            ),
            {"i": candidate_id, "t": tenant_id, "o": ann},
        )
        s.add(
            SourcingRun(
                id=run_id,
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                state=SourcingRun.DONE,
                created_by=bea,
            )
        )
        await s.flush()
        s.add(
            SourcingMatch(
                tenant_id=tenant_id,
                run_id=run_id,
                candidate_id=candidate_id,
                score=0.8125,
                reasons=[REASON],
                explanation=EXPLANATION,
                explanation_evidence=EVIDENCE,
            )
        )
        await s.commit()

    yield tenant_id, ann, bea, opportunity_id, run_id, candidate_id

    async with AdminSessionLocal() as s:
        for table in (
            "sourcing_matches",
            "sourcing_runs",
            "candidate_shares",
            "candidates",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()
    await cleanup_tenant(tenant_id)


def _http(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, user_id, tenant_id)
    return client


async def _share_candidate_with(tenant_id, candidate_id, user_id, by_user_id) -> None:
    async with AdminSessionLocal() as s:
        s.add(
            CandidateShare(
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                scope=CandidateShare.SCOPE_USER,
                shared_with_user_id=user_id,
                shared_by_user_id=by_user_id,
            )
        )
        await s.commit()


def _withheld(body: str) -> list[str]:
    return [s for s in (EXPLANATION, EVIDENCE, REASON) if s in body]


# --- redacted --------------------------------------------------------------


async def test_a_privately_held_candidate_is_present_but_redacted(agency) -> None:
    """The match stays — dropping it would gut the product — and says only
    what the 409 collision body says: an abbreviated name, who holds her, the
    id the access-request endpoint is keyed by."""
    tenant_id, _ann, bea, opportunity_id, run_id, candidate_id = agency

    async with _http(tenant_id, bea) as http:
        response = await http.get(
            f"/api/opportunities/{opportunity_id}/sourcing/{run_id}"
        )

    assert response.status_code == 200, response.text
    # First, because it is the finding itself: before the fix the response
    # carried all three of these for a candidate Bea may not see.
    assert _withheld(response.text) == [], response.text

    matches = response.json()["matches"]
    assert [m["candidate_id"] for m in matches] == [str(candidate_id)]

    match = matches[0]
    assert match["visible"] is False
    assert match["full_name"] == "Siti Nur R."
    assert match["held_by"] == "Ann Lim"
    assert match["can_request_access"] is True
    # The score reveals fit, not content, so it survives redaction.
    assert match["score"] == "0.8125"


async def test_the_latest_shortlist_redacts_the_same_way(agency) -> None:
    """Two call sites read matches. Fixing one and not the other would leave
    the leak reachable by the route a recruiter actually opens."""
    tenant_id, _ann, bea, opportunity_id, _run_id, _candidate_id = agency

    async with _http(tenant_id, bea) as http:
        response = await http.get(f"/api/opportunities/{opportunity_id}/sourcing")

    assert response.status_code == 200, response.text
    assert response.json()["matches"][0]["visible"] is False
    assert _withheld(response.text) == [], response.text


# --- disclosed -------------------------------------------------------------


async def test_sharing_the_candidate_reopens_the_same_stored_run(agency) -> None:
    """The positive half. Nothing is rescored: the run is the one already
    stored, and only the viewer changed."""
    tenant_id, ann, bea, opportunity_id, run_id, candidate_id = agency
    await _share_candidate_with(tenant_id, candidate_id, bea, ann)

    async with _http(tenant_id, bea) as http:
        response = await http.get(
            f"/api/opportunities/{opportunity_id}/sourcing/{run_id}"
        )

    assert response.status_code == 200, response.text
    match = response.json()["matches"][0]
    assert match["visible"] is True
    assert match["explanation"] == EXPLANATION
    assert match["explanation_evidence"] == EVIDENCE
    assert match["reasons"] == [REASON]
    assert sorted(_withheld(response.text)) == sorted([EXPLANATION, EVIDENCE, REASON])


async def test_the_owner_of_the_candidate_sees_their_own_detail(agency) -> None:
    """Ann holds the candidate but not the job order, so she reaches this run
    only once the job order is shared with her. Rather than share it, assert
    the narrower thing directly: redaction keys on the *candidate's* holder,
    so a candidate nobody owns is not redacted either."""
    tenant_id, _ann, bea, opportunity_id, run_id, candidate_id = agency
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE candidates SET owner_id = NULL WHERE id = :i"),
            {"i": candidate_id},
        )
        await s.commit()

    async with _http(tenant_id, bea) as http:
        response = await http.get(
            f"/api/opportunities/{opportunity_id}/sourcing/{run_id}"
        )

    assert response.status_code == 200, response.text
    assert response.json()["matches"][0]["visible"] is True
    assert EXPLANATION in response.text
