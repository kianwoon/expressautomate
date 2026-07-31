"""Sharing a candidate: the table's own guarantees, before any route exists."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api import candidate_shares
from app.models.candidate_share import CandidateShare
from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_a_tenant_share_must_have_no_recipient(admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-share-pairing")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_TENANT,
            shared_with_user_id=owner,  # illegal: a broadcast names nobody
        )
    )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_deleting_a_recipient_deletes_their_shares(admin_session, seeded) -> None:
    """CASCADE, not SET NULL. SET NULL would turn a targeted share into a
    broadcast and violate ck_candidate_shares_scope_target, making the user
    DELETE fail outright."""
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-share-cascade")
    recipient = await make_user(admin_session, tenant_id, "colleague@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=recipient,
            shared_by_user_id=owner,
        )
    )
    await admin_session.commit()

    await admin_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": recipient})
    await admin_session.commit()

    left = (
        await admin_session.execute(
            text("SELECT count(*) AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one()
    assert left.n == 0


@pytest.mark.asyncio
async def test_a_share_cannot_reach_another_agency(admin_session, seeded) -> None:
    """Refused by the composite foreign key, not merely by application code."""
    make_tenant, _, _ = seeded
    tenant_a, owner_a, _ = await make_tenant("agency-a-cross")
    tenant_b, owner_b, _ = await make_tenant("agency-b-cross")
    candidate_id = await make_candidate(admin_session, tenant_a, owner_id=owner_a)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=owner_b,  # a user in the other agency
            shared_by_user_id=owner_a,
        )
    )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_only_one_pending_request_per_person(admin_session, seeded) -> None:
    """A recruiter clicking twice must not spam the owner."""
    from app.models.candidate_access_request import CandidateAccessRequest

    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-request-dedupe")
    asker = await make_user(admin_session, tenant_id, "asker@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    for _ in range(2):
        admin_session.add(
            CandidateAccessRequest(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                requested_by_user_id=asker,
                status=CandidateAccessRequest.STATUS_PENDING,
            )
        )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_a_recipient_may_share_onward_but_not_broadcast(
    client, admin_session, seeded
) -> None:
    """A candidate finds the right person through a chain of individual
    shares. Throwing a colleague's candidate open to the whole office is not
    the recipient's decision to make."""
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-onward")
    recipient = await make_user(admin_session, tenant_id, "r@agency.test")
    third = await make_user(admin_session, tenant_id, "t@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=recipient,
            shared_by_user_id=owner,
        )
    )
    await admin_session.commit()

    sign_in(client, recipient, tenant_id)
    onward = await client.post(
        f"/api/candidates/{candidate_id}/shares",
        json={"scope": "user", "user_ids": [str(third)]},
    )
    assert onward.status_code == 201

    broadcast = await client.post(
        f"/api/candidates/{candidate_id}/shares", json={"scope": "tenant"}
    )
    assert broadcast.status_code == 403


@pytest.mark.asyncio
async def test_resharing_updates_the_note_rather_than_409ing(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-reshare")
    colleague = await make_user(admin_session, tenant_id, "c@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    await admin_session.commit()

    sign_in(client, owner, tenant_id)
    body = {"scope": "user", "user_ids": [str(colleague)], "note": "first"}
    url = f"/api/candidates/{candidate_id}/shares"
    assert (await client.post(url, json=body)).status_code == 201
    body["note"] = "second"
    assert (await client.post(url, json=body)).status_code == 201

    row = (
        await admin_session.execute(
            text(
                "SELECT note, count(*) OVER () AS n FROM candidate_shares "
                "WHERE candidate_id = :c"
            ),
            {"c": candidate_id},
        )
    ).one()
    assert row.note == "second"
    assert row.n == 1, "re-sharing duplicated instead of updating — check index_where"


@pytest.mark.asyncio
async def test_requesting_access_leaks_nothing_and_granting_creates_one_share(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-request-flow")
    asker = await make_user(admin_session, tenant_id, "asker2@agency.test")
    candidate_id = await make_candidate(
        admin_session, tenant_id, owner_id=owner, full_name="Secret Person"
    )
    await admin_session.commit()

    sign_in(client, asker, tenant_id)
    # Invisible right now.
    assert (await client.get(f"/api/candidates/{candidate_id}")).status_code == 404

    asked = await client.post(
        f"/api/candidates/{candidate_id}/access-requests", json={"note": "same person I met"}
    )
    assert asked.status_code == 200
    assert "Secret Person" not in asked.text, "the request route revealed the row"
    request_id = asked.json()["id"]

    # A second click must not spam the owner.
    assert (
        await client.post(f"/api/candidates/{candidate_id}/access-requests", json={})
    ).status_code == 200

    sign_in(client, owner, tenant_id)
    inbox = await client.get("/api/candidates/access-requests?status=pending")
    assert inbox.status_code == 200
    assert len(inbox.json()["items"]) == 1

    granted = await client.post(
        f"/api/candidates/{candidate_id}/access-requests/{request_id}/grant"
    )
    assert granted.status_code == 200

    shares = (
        await admin_session.execute(
            text("SELECT count(*) AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one()
    assert shares.n == 1

    sign_in(client, asker, tenant_id)
    assert (await client.get(f"/api/candidates/{candidate_id}")).status_code == 200


@pytest.mark.asyncio
async def test_a_declined_request_may_be_asked_again(
    client, admin_session, seeded, monkeypatch
) -> None:
    """The unique index is PARTIAL — `status = 'pending'` — and this is the
    only thing that proves why. Circumstances change: a recruiter refused in
    March may have a live vacancy for the same person in June, and a total
    index would make the second ask a 500 forever.

    It also asserts the decline is delivered rather than silently swallowed:
    a request that never resolves leaves the asker believing it is open.
    """
    from app.models.candidate_access_request import CandidateAccessRequest

    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-reask")
    asker = await make_user(admin_session, tenant_id, "reasker@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    await admin_session.commit()

    sign_in(client, asker, tenant_id)
    first = await client.post(
        f"/api/candidates/{candidate_id}/access-requests", json={"note": "March"}
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    # Captured at the module the route calls it from, so this asserts the
    # decline is actually announced. Declining is not optional politeness: a
    # request that silently never resolves leaves the asker believing it is
    # open, and they ask again.
    emitted: list[object] = []

    async def _capture(event):
        emitted.append(event)
        return 0

    monkeypatch.setattr(candidate_shares, "emit_candidate_event", _capture)

    sign_in(client, owner, tenant_id)
    declined = await client.post(
        f"/api/candidates/{candidate_id}/access-requests/{first_id}/decline"
    )
    assert declined.status_code == 200
    assert [e.kind for e in emitted] == ["candidate.access_declined"], emitted
    # To the requester, not to the office.
    assert emitted[0].recipient_user_ids == (asker,)
    # It reveals nothing about the row it refers to.
    assert emitted[0].candidate_name is None

    resolved = (
        await admin_session.execute(
            text(
                "SELECT status, resolved_at, resolved_by_user_id "
                "FROM candidate_access_requests WHERE id = :i"
            ),
            {"i": first_id},
        )
    ).one()
    assert resolved.status == CandidateAccessRequest.STATUS_DECLINED
    # The paired-nullability CHECK: a resolution has a time.
    assert resolved.resolved_at is not None
    assert resolved.resolved_by_user_id == owner

    # Declining creates nothing.
    assert (
        await admin_session.execute(
            text("SELECT count(*) AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one().n == 0

    sign_in(client, asker, tenant_id)
    # June. The partial index leaves the declined row uncovered, so a NEW
    # pending request is accepted rather than colliding with the old one.
    again = await client.post(
        f"/api/candidates/{candidate_id}/access-requests", json={"note": "June"}
    )
    assert again.status_code == 200
    assert again.json()["id"] != first_id, "re-asking reopened the declined row"

    rows = (
        await admin_session.execute(
            text(
                "SELECT status FROM candidate_access_requests "
                "WHERE candidate_id = :c ORDER BY created_at"
            ),
            {"c": candidate_id},
        )
    ).scalars().all()
    assert sorted(rows) == ["declined", "pending"], rows
