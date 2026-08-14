"""Editing a job order's own fields (`PATCH /api/opportunities/{id}`).

A recruiter corrects what the email said — a salary figure, a location, the
requirements — from the detail panel. Four rules are pinned here:

- The edit guard, not the visibility one: a shared row is readable but not
  editable, and a queue row is claimable but not editable. Claiming is the act
  that creates edit rights.
- A raw salary string drives the structured columns through the same
  deterministic parser the email pipeline uses, so the list sorts on the
  corrected figure — and a clear back to "not stated" clears the range too.
- A human correction lands in `opportunity_field_overrides`, the same table
  replay reads before refreshing a row, so a re-run of the source email never
  silently undoes a recruiter's fix.
- Agency isolation holds on writes the same way it holds on reads.

allow-hardcode: the strings below are test fixture data and verbatim copies of
user-facing labels, not a detection or scoring oracle.
"""

import uuid

from sqlalchemy import text

from app.db.rls import tenant_session
from tests.conftest import AdminSessionLocal, sign_in


async def test_editing_fields_round_trips(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme Pte Ltd",
        job_title_raw="Care assistant",
        location_raw="West",
        salary_raw="$2,000/month",
        salary_min=2000,
        salary_max=2000,
        salary_currency="SGD",
        salary_period="month",
        # Assigned: only the assignee (or the owner) may edit the fields.
        assigned_user_id=user_id,
    )

    sign_in(client, user_id, tenant_id)
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}",
        json={
            "location_raw": "North",
            "job_title_raw": "Senior care assistant",
            "requirements": "At least 2 years experience.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(opportunity_id)
    assert body["location_raw"] == "North"
    assert body["job_title_raw"] == "Senior care assistant"
    assert body["requirements"] == "At least 2 years experience."
    # The untouched fields survive.
    assert body["company_name_raw"] == "Acme Pte Ltd"

    # The list agrees immediately — the payload IS the list's row shape.
    listed = (await client.get("/api/opportunities")).json()
    row = next(r for r in listed["items"] if r["id"] == str(opportunity_id))
    assert row["location_raw"] == "North"
    assert row["job_title_raw"] == "Senior care assistant"


async def test_salary_raw_drives_the_structured_columns(client, seeded) -> None:
    """The raw string a recruiter types is the source; the min/max/currency
    are derived from it the same way the email pipeline derives them."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme", assigned_user_id=user_id
    )

    sign_in(client, user_id, tenant_id)
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}",
        json={"salary_raw": "SGD 4,500–5,000/month"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["salary_raw"] == "SGD 4,500–5,000/month"
    assert body["salary_min"] == 4500
    assert body["salary_max"] == 5000
    assert body["salary_currency"] == "SGD"
    # The period is derived from the words the recruiter typed, so the row
    # sorts by the salary it now claims even though the form sent no period.
    assert body["salary_period"] == "month"


async def test_clearing_salary_raw_clears_the_range(client, seeded) -> None:
    """A blank salary is not a figure — the range has to disappear with it,
    or the list would sort on a number the row no longer claims."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme",
        salary_raw="$2,000/month",
        salary_min=2000,
        salary_max=2000,
        salary_currency="SGD",
        salary_period="month",
        assigned_user_id=user_id,
    )

    sign_in(client, user_id, tenant_id)
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}", json={"salary_raw": None}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["salary_raw"] is None
    assert body["salary_min"] is None
    assert body["salary_max"] is None
    assert body["salary_currency"] is None
    assert body["salary_period"] is None


async def test_a_human_correction_is_recorded_as_an_override(client, seeded) -> None:
    """The whole point of the override table: replay must never overwrite a
    recruiter's fix, so the fix has to be recorded where replay looks."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme Pte Ltd",
        job_title_raw="Care assistant",
        assigned_user_id=user_id,
    )

    sign_in(client, user_id, tenant_id)
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}",
        json={"job_title_raw": "Senior care assistant"},
    )
    assert response.status_code == 200

    async with tenant_session(tenant_id) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT field_name, ai_value, human_value, corrected_by "
                    "FROM opportunity_field_overrides WHERE opportunity_id = :o"
                ),
                {"o": opportunity_id},
            )
        ).all()
    assert rows == [
        ("job_title_raw", "Care assistant", "Senior care assistant", user_id)
    ]


async def test_echoing_a_value_back_unchanged_is_not_an_override(client, seeded) -> None:
    """Sending the row back as-is (a form that posts everything) is not an
    edit. Recording it would freeze the field from every later replay for a
    decision nobody made."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme Pte Ltd",
        job_title_raw="Care assistant",
        assigned_user_id=user_id,
    )

    sign_in(client, user_id, tenant_id)
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}",
        json={"job_title_raw": "Care assistant"},
    )
    assert response.status_code == 200

    async with tenant_session(tenant_id) as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM opportunity_field_overrides "
                    "WHERE opportunity_id = :o"
                ),
                {"o": opportunity_id},
            )
        ).scalar_one()
    assert count == 0


async def test_a_shared_job_order_is_readable_but_not_editable(client, seeded) -> None:
    """The edit guard, not the visibility guard: sharing grants sight, not
    the right to change the row."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, assignee, mailbox_id = await make_tenant("agency-a")
    from tests.test_opportunity_visibility_routes import _colleague

    colleague = await _colleague(tenant_id)
    # The email goes through the colleague's own mailbox, so the assignee's
    # only claim on the row is the share — mirroring the `colleagues_job_order`
    # fixture, which re-points the mail to avoid the reader being visible for a
    # reason that has nothing to do with sharing.
    colleague_mailbox = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                " VALUES (:i, :t, :u, :m, 'user', 'inbox', 12)"
            ),
            {
                "i": colleague_mailbox,
                "t": tenant_id,
                "u": colleague,
                "m": f"oid-{colleague.hex[:8]}",
            },
        )
        await s.commit()
    opportunity_id = await make_opportunity(
        tenant_id,
        colleague_mailbox,
        company_name_raw="Acme Pte Ltd",
        job_title_raw="Care assistant",
        assigned_user_id=assignee,
    )

    # The assignee shares the row with the colleague.
    sign_in(client, assignee, tenant_id)
    shared = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)], "note": "you know Acme"},
    )
    assert shared.status_code == 200, shared.text

    sign_in(client, colleague, tenant_id)
    # It is visible — the panel renders it.
    visible = await client.get(f"/api/opportunities/{opportunity_id}")
    assert visible.status_code == 200

    # But editing it is refused.
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}", json={"location_raw": "North"}
    )
    assert response.status_code == 403

    async with tenant_session(tenant_id) as s:
        stored = (
            await s.execute(
                text("SELECT location_raw FROM opportunities WHERE id = :o"),
                {"o": opportunity_id},
            )
        ).scalar_one()
    assert stored is None


async def test_an_unassigned_job_order_must_be_claimed_before_editing(
    client, seeded,
) -> None:
    """The other half of the edit guard: a queue row is visible and claimable
    but not editable. Claiming is the act that creates the right to fix it."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd"
    )

    sign_in(client, user_id, tenant_id)
    response = await client.patch(
        f"/api/opportunities/{opportunity_id}", json={"location_raw": "North"}
    )
    assert response.status_code == 403


async def test_editing_another_agencys_vacancy_is_a_404(client, seeded) -> None:
    """The write side of isolation, and it is RLS that supplies it."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_a, user_a, _mailbox_a = await make_tenant("agency-a")
    tenant_b, _user_b, mailbox_b = await make_tenant("agency-b")
    theirs = await make_opportunity(tenant_b, mailbox_b, company_name_raw="Rival Holdings")

    sign_in(client, user_a, tenant_a)
    response = await client.patch(
        f"/api/opportunities/{theirs}", json={"location_raw": "North"}
    )
    assert response.status_code == 404


async def test_an_anonymous_caller_cannot_edit(client) -> None:
    response = await client.patch(
        f"/api/opportunities/{uuid.uuid4()}", json={"location_raw": "North"}
    )
    assert response.status_code == 401
