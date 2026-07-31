"""Who owns this candidate, and may the reader edit it.

The share recipient's edit control is DISABLED, not hidden — which the UI can
only draw if the payload says both who holds the row and that this reader
cannot change it. `can_edit` is `can_edit_candidate` verbatim; it is not
re-derived here or in the serializer.
"""

import uuid

import pytest

from app.models.candidate_share import CandidateShare
from tests.conftest import make_candidate, make_user, sign_in


async def _share_with(session, tenant_id, candidate_id, owner, recipient) -> None:
    session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=recipient,
            shared_by_user_id=owner,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_a_share_recipient_sees_the_owner_and_cannot_edit(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-owner-payload")
    recipient = await make_user(admin_session, tenant_id, "recipient@agency.test")
    candidate_id = await make_candidate(
        admin_session, tenant_id, owner_id=owner, full_name="Shared Person"
    )
    await _share_with(admin_session, tenant_id, candidate_id, owner, recipient)
    await admin_session.commit()

    sign_in(client, recipient, tenant_id)
    detail = await client.get(f"/api/candidates/{candidate_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["can_edit"] is False
    assert body["owner"]["id"] == str(owner)
    assert body["owner"]["name"]

    listed = await client.get("/api/candidates?scope=shared_with_me")
    assert listed.status_code == 200
    row = next(r for r in listed.json()["items"] if r["id"] == str(candidate_id))
    assert row["can_edit"] is False
    assert row["owner"] == body["owner"]


@pytest.mark.asyncio
async def test_the_owner_and_the_agency_owner_may_edit(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, holder, _ = await make_tenant("agency-owner-payload-edit")
    boss = await make_user(admin_session, tenant_id, "boss@agency.test", role="owner")
    candidate_id = await make_candidate(
        admin_session, tenant_id, owner_id=holder, full_name="Held Person"
    )
    await admin_session.commit()

    sign_in(client, holder, tenant_id)
    assert (await client.get(f"/api/candidates/{candidate_id}")).json()["can_edit"] is True

    sign_in(client, boss, tenant_id)
    seen = (await client.get(f"/api/candidates/{candidate_id}")).json()
    assert seen["can_edit"] is True
    assert seen["owner"]["id"] == str(holder)


@pytest.mark.asyncio
async def test_the_owner_name_prefers_what_the_person_chose_to_be_called(
    client, admin_session, seeded
) -> None:
    """The same coalesce `held_by_colleague` uses — preferred, then display,
    then the address they signed in with. Never a bare UUID."""
    from sqlalchemy import text

    make_tenant, _, _ = seeded
    tenant_id, holder, _ = await make_tenant("agency-owner-name")
    reader = await make_user(admin_session, tenant_id, "reader@agency.test", role="owner")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=holder)
    await admin_session.execute(
        text("UPDATE users SET preferred_name = :p, display_name = :d WHERE id = :i"),
        {"p": "Mei", "d": "Mei Ling Ong", "i": holder},
    )
    await admin_session.commit()

    sign_in(client, reader, tenant_id)
    assert (await client.get(f"/api/candidates/{candidate_id}")).json()["owner"]["name"] == "Mei"

    await admin_session.execute(
        text("UPDATE users SET preferred_name = NULL WHERE id = :i"), {"i": holder}
    )
    await admin_session.commit()
    listed = await client.get("/api/candidates")
    row = next(r for r in listed.json()["items"] if r["id"] == str(candidate_id))
    assert row["owner"]["name"] == "Mei Ling Ong"


@pytest.mark.asyncio
async def test_an_unclaimed_candidate_has_no_owner_and_no_edit(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-unclaimed-owner")
    recruiter = await make_user(admin_session, tenant_id, "recruiter@agency.test")
    candidate_id = await make_candidate(
        admin_session, tenant_id, owner_id=None, full_name="Nobody Holds Me"
    )
    await admin_session.commit()
    assert me  # the seeded user; the recruiter below is an ordinary role

    sign_in(client, recruiter, tenant_id)
    body = (await client.get(f"/api/candidates/{candidate_id}")).json()
    assert body["owner"] is None
    assert body["can_edit"] is False

    listed = await client.get("/api/candidates")
    row = next(r for r in listed.json()["items"] if r["id"] == str(candidate_id))
    assert row["owner"] is None
    assert row["can_edit"] is False


@pytest.mark.asyncio
async def test_the_owner_join_does_not_add_drop_or_reorder_a_row(
    client, admin_session, seeded
) -> None:
    """The join is a LEFT OUTER onto `users.id`, a primary key, so it is
    one-to-one and cannot fan out; and it is applied only to the row fetch,
    not to the COUNT or the initials aggregate.

    Asserted rather than argued: an owned row, an unclaimed row (the case an
    INNER join would silently drop) and a second owned row must all appear,
    exactly once each, in the documented `updated_at DESC, id DESC` order,
    with `total` agreeing with what came back.
    """
    from sqlalchemy import text

    make_tenant, _, _ = seeded
    tenant_id, _seeded_user, _ = await make_tenant("agency-owner-join")
    boss = await make_user(admin_session, tenant_id, "join-boss@agency.test", role="owner")
    holder = await make_user(admin_session, tenant_id, "join-holder@agency.test")
    ids = [
        await make_candidate(admin_session, tenant_id, owner_id=holder, full_name="A"),
        await make_candidate(admin_session, tenant_id, owner_id=None, full_name="B"),
        await make_candidate(admin_session, tenant_id, owner_id=boss, full_name="C"),
    ]
    await admin_session.commit()

    sign_in(client, boss, tenant_id)
    payload = (await client.get("/api/candidates")).json()
    returned = [r["id"] for r in payload["items"]]

    assert len(returned) == len(set(returned)) == 3
    assert payload["total"] == 3
    assert payload["counts"]["all"] == 3
    # The order the un-joined query would have produced, taken straight from
    # the database, must be the order the joined one returns.
    expected = [
        str(row.id)
        for row in await admin_session.execute(
            text(
                "SELECT id FROM candidates WHERE tenant_id = :t "
                "AND record_status <> 'merged' ORDER BY updated_at DESC, id DESC"
            ),
            {"t": tenant_id},
        )
    ]
    assert sorted(expected) == sorted(str(i) for i in ids)
    assert returned == expected
