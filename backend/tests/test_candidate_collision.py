"""Two recruiters, one person — the moment they find out.

Per-tenant email/phone uniqueness is unchanged, so the second recruiter to
type an email cannot create a row. What they get instead is the whole design
decision: enough to act on, and nothing more.
"""

import pytest

from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_creating_a_candidate_a_colleague_holds_returns_a_thin_409(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, colleague, _ = await make_tenant("agency-collision")
    me = await make_user(admin_session, tenant_id, "me@agency.test")
    await make_candidate(
        admin_session,
        tenant_id,
        owner_id=colleague,
        full_name="Wei Ming Tan",
        email="weiming@example.com",
        phone_e164="+6591234567",
        current_title="Senior Backend Engineer",
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        "/api/candidates",
        json={"full_name": "Wei Ming Tan", "email": "weiming@example.com"},
    )

    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["reason"] == "already_registered"
    assert body["can_request_access"] is True
    # The disclosure is deliberate and bounded: who holds them, and a name
    # short enough to recognise. Nothing else crosses the boundary.
    assert set(body["candidate"]) == {"full_name", "held_by"}
    assert "+6591234567" not in response.text
    assert "Senior Backend Engineer" not in response.text


@pytest.mark.asyncio
async def test_a_conflicting_match_is_not_a_collision(client, admin_session, seeded) -> None:
    """Email and phone pointing at two different people. The system does not
    know which person is meant, so it cannot name one — and offers no
    request-access."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-conflict")
    await make_candidate(
        admin_session, tenant_id, owner_id=me, email="a@example.com", phone_e164="+6590000001"
    )
    await make_candidate(
        admin_session, tenant_id, owner_id=me, email="b@example.com", phone_e164="+6590000002"
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        "/api/candidates",
        json={"full_name": "Someone", "email": "a@example.com", "phone_raw": "+6590000002"},
    )
    assert response.status_code == 409
    assert "can_request_access" not in response.text


@pytest.mark.asyncio
async def test_patching_an_email_onto_a_colleagues_candidate_returns_409_not_500(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, colleague, _ = await make_tenant("agency-patch-collision")
    me = await make_user(admin_session, tenant_id, "me2@agency.test")
    await make_candidate(
        admin_session,
        tenant_id,
        owner_id=colleague,
        full_name="Wei Ming Tan",
        email="weiming@example.com",
    )
    mine = await make_candidate(
        admin_session, tenant_id, owner_id=me, full_name="Wei M Tan", email="typo@example.com"
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.patch(
        f"/api/candidates/{mine}", json={"email": "weiming@example.com"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "already_registered"
    assert "uq_candidates_tenant_email" not in response.text


@pytest.mark.asyncio
async def test_patching_without_touching_the_email_is_not_a_collision(
    client, admin_session, seeded
) -> None:
    """The row being edited matches itself. That is not a collision, and
    treating it as one would make every edit impossible."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-patch-self")
    mine = await make_candidate(
        admin_session, tenant_id, owner_id=me, email="self@example.com"
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.patch(f"/api/candidates/{mine}", json={"current_title": "CTO"})
    assert response.status_code == 200
