"""404 when invisible, 403 when visible but not editable — over real routes.

The subtlest rule in the feature, and the one a refactor inverts by accident:
both codes mean "no", so swapping them breaks nothing a caller notices until
somebody uses the difference to enumerate ids. A 403 on an INVISIBLE candidate
confirms the row exists, which is the disclosure the 404 is there to prevent;
a 404 on a candidate a colleague deliberately SHARED tells the recipient the
record vanished.

`tests/test_candidate_visibility.py` covers the predicate and
`can_edit_candidate` as functions. This covers the two HTTP status codes the
guards actually return, because the inversion is invisible at the function
level: both helpers raise, and only the status number differs.
"""

import uuid

import pytest

from app.models.candidate_share import CandidateShare
from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_an_invisible_candidate_is_404_and_a_shared_one_is_403(
    client, admin_session, seeded
) -> None:
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, _seed_user, _mailbox = await make_tenant("agency-404-vs-403")

    me = await make_user(admin_session, tenant_id, "me@agency-404-vs-403.test")
    colleague = await make_user(
        admin_session, tenant_id, "colleague@agency-404-vs-403.test"
    )

    # Two candidates the colleague owns. Identical in every way except that
    # one of them is shared with me — so the only thing the two responses can
    # be telling apart is the share.
    invisible = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    shared = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=shared,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=me,
            shared_by_user_id=colleague,
        )
    )
    # commit(), not flush(): the routes below open their own sessions, and an
    # uncommitted fixture row would deadlock against the row locks they take.
    await admin_session.commit()

    sign_in(client, me, tenant_id)

    # Invisible: the read guard must not admit the row exists.
    assert (await client.get(f"/api/candidates/{invisible}")).status_code == 404
    # And nor may a write, which is the tempting place to return 403.
    patched = await client.patch(
        f"/api/candidates/{invisible}", json={"current_title": "Nope"}
    )
    assert patched.status_code == 404, patched.text

    # Shared: readable...
    assert (await client.get(f"/api/candidates/{shared}")).status_code == 200
    # ...and 403 on the edit, NOT 404. I can already see this candidate, so
    # concealing it would be theatre and would read as the record vanishing.
    refused = await client.patch(
        f"/api/candidates/{shared}", json={"current_title": "Nope"}
    )
    assert refused.status_code == 403, refused.text


@pytest.mark.asyncio
async def test_a_candidate_nobody_owns_is_visible_but_not_editable(
    client, admin_session, seeded
) -> None:
    """The unclaimed queue. Visible to everyone by design, so the refusal here
    must be 403 — a 404 would hide the queue this recruiter is meant to work."""
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, _seed_user, _mailbox = await make_tenant("agency-unclaimed")
    me = await make_user(admin_session, tenant_id, "me@agency-unclaimed.test")
    unowned = await make_candidate(admin_session, tenant_id, owner_id=None)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (await client.get(f"/api/candidates/{unowned}")).status_code == 200
    refused = await client.patch(
        f"/api/candidates/{unowned}", json={"current_title": "Nope"}
    )
    assert refused.status_code == 403, refused.text
