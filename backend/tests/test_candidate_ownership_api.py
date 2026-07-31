"""Claiming from the queue, and handing a candidate over."""

import uuid

import pytest
from sqlalchemy import text

from app.models.notification import CHANNEL_TELEGRAM, address_digest
from tests.conftest import make_candidate, make_user, sign_in


async def _subscribe(admin_session, tenant_id, user_id, event_kind) -> None:
    """A destination that will actually receive.

    `notification_deliveries` is the outbox — there is no `notification_
    outbox` table — and a row only lands there for a *subscriber*, so the
    event is unobservable without one.
    """
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'ciphertext', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest(f"tg-{dest_id}"),
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_subscriptions "
            "(id, tenant_id, destination_id, event_kind, active) "
            "VALUES (:id, :tid, :did, :kind, true)"
        ),
        {"id": uuid.uuid4(), "tid": tenant_id, "did": dest_id, "kind": event_kind},
    )


@pytest.mark.asyncio
async def test_two_claims_produce_one_winner(client, admin_session, seeded) -> None:
    """The race two recruiters will genuinely hit in a 9pm rush."""
    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-claim-race")
    second = await make_user(admin_session, tenant_id, "second@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=None)
    await admin_session.commit()

    sign_in(client, first, tenant_id)
    one = await client.post(f"/api/candidates/{candidate_id}/claim")
    sign_in(client, second, tenant_id)
    two = await client.post(f"/api/candidates/{candidate_id}/claim")

    assert sorted([one.status_code, two.status_code]) == [200, 409]


@pytest.mark.asyncio
async def test_scope_filters_cannot_widen_visibility(client, admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-scope")
    colleague = await make_user(admin_session, tenant_id, "other@agency.test")
    for owner in (me, colleague, None):
        await make_candidate(admin_session, tenant_id, owner_id=owner)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    everything = {
        row["id"] for row in (await client.get("/api/candidates?scope=all")).json()["items"]
    }
    for scope in ("mine", "queue", "shared_with_me"):
        subset = {
            row["id"]
            for row in (await client.get(f"/api/candidates?scope={scope}")).json()["items"]
        }
        assert subset <= everything, f"scope={scope} widened visibility"


@pytest.mark.asyncio
async def test_scope_mine_and_queue_actually_narrow(client, admin_session, seeded) -> None:
    """`subset <= everything` alone would pass for a filter that did nothing."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-scope-narrow")
    colleague = await make_user(admin_session, tenant_id, "narrow@agency.test")
    mine = await make_candidate(admin_session, tenant_id, owner_id=me)
    unowned = await make_candidate(admin_session, tenant_id, owner_id=None)
    await make_candidate(admin_session, tenant_id, owner_id=colleague)
    await admin_session.commit()

    sign_in(client, me, tenant_id)

    async def ids(scope: str) -> set[str]:
        body = (await client.get(f"/api/candidates?scope={scope}")).json()
        return {row["id"] for row in body["items"]}

    assert await ids("mine") == {str(mine)}
    assert await ids("queue") == {str(unowned)}
    # A colleague's owned candidate is invisible either way — the predicate,
    # not the scope filter, is what excludes it.
    assert await ids("all") == {str(mine), str(unowned)}


@pytest.mark.asyncio
async def test_releasing_to_the_queue_tells_the_agency(client, admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-release")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=me)
    await _subscribe(admin_session, tenant_id, me, "candidate.unclaimed")
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (
        await client.post(f"/api/candidates/{candidate_id}/assign", json={"user_id": None})
    ).status_code == 200

    kinds = (
        (
            await admin_session.execute(
                text("SELECT event_kind FROM notification_deliveries WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    assert "candidate.unclaimed" in kinds


@pytest.mark.asyncio
async def test_claiming_emits_nothing(client, admin_session, seeded) -> None:
    """You did it, you know."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-claim-quiet")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=None)
    for kind in ("candidate.assigned", "candidate.unclaimed"):
        await _subscribe(admin_session, tenant_id, me, kind)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (await client.post(f"/api/candidates/{candidate_id}/claim")).status_code == 200

    written = (
        await admin_session.execute(
            text("SELECT count(*) FROM notification_deliveries WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar_one()
    assert written == 0


@pytest.mark.asyncio
async def test_handing_over_tells_only_the_new_owner(client, admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-handover")
    colleague = await make_user(admin_session, tenant_id, "handover@agency.test")
    bystander = await make_user(admin_session, tenant_id, "bystander@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=me)
    for who in (colleague, bystander):
        await _subscribe(admin_session, tenant_id, who, "candidate.assigned")
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (
        await client.post(
            f"/api/candidates/{candidate_id}/assign", json={"user_id": str(colleague)}
        )
    ).status_code == 200

    recipients = (
        (
            await admin_session.execute(
                text(
                    "SELECT d.user_id FROM notification_deliveries n "
                    "JOIN notification_destinations d ON d.id = n.destination_id "
                    "WHERE n.tenant_id = :t"
                ),
                {"t": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    assert recipients == [colleague]

    owner = (
        await admin_session.execute(
            text("SELECT owner_id FROM candidates WHERE id = :c"), {"c": candidate_id}
        )
    ).scalar_one()
    assert owner == colleague


@pytest.mark.asyncio
async def test_a_share_recipient_cannot_hand_the_candidate_on(
    client, admin_session, seeded
) -> None:
    """Visible is not editable. Assigning somebody else's candidate is an edit."""
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-not-yours")
    other = await make_user(admin_session, tenant_id, "notyours@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    await admin_session.commit()

    sign_in(client, other, tenant_id)
    response = await client.post(
        f"/api/candidates/{candidate_id}/assign", json={"user_id": str(other)}
    )
    # 404, not 403: an owned candidate nobody shared is not visible at all.
    assert response.status_code == 404
