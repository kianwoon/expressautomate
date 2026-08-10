# allow-hardcode: the names, titles and SQL below are test fixture content,
# not an oracle and not configuration.
"""Find Job: the job orders that best fit one candidate.

The reverse of sourcing, so the questions mirror `test_sourcing_api.py`'s but
point the other way. Can a caller reach another agency's candidate. Does the
shortlist draw only on job orders this recruiter can see. Is a superseded
revision hidden in favour of its replacement. And does a vacancy with nothing
comparable come back as "not scored" rather than as a 0% embarrassment.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal
from tests.test_opportunities_api import sign_in


async def _seed_tenant(role: str = "owner") -> tuple[uuid.UUID, uuid.UUID]:
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, :r)"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg", "r": role},
        )
        await s.commit()
    return tid, uid


async def _drop_tenant(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in (
            "candidate_skills",
            "candidate_roles",
            "candidates",
            "opportunities",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.fixture
async def agency():
    tid, uid = await _seed_tenant("owner")
    yield tid, uid
    await _drop_tenant(tid)


@pytest.fixture
async def other_agency():
    tid, uid = await _seed_tenant("owner")
    yield tid, uid
    await _drop_tenant(tid)


async def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, uid, tid)
    return client


async def _candidate(
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    title: str = "Staff Nurse",
    employer: str = "Acme Health",
    email: str | None = None,
) -> uuid.UUID:
    """A candidate with one dated role and two skills, so every structured
    component has something to compare."""
    cid = uuid.uuid4()
    role_id = uuid.uuid4()
    # Unique by default: several tests hold more than one candidate in the
    # same tenant, and email uniqueness is per tenant (`uq_candidates_tenant_email`).
    if email is None:
        email = f"nur-{cid.hex[:8]}@example.sg"
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email,"
                " expected_salary, salary_currency, salary_period, pipeline_stage,"
                " record_status, owner_id, created_by, updated_by)"
                " VALUES (:i, :t, 'Nur Aisyah', :e, 3000, 'SGD',"
                " 'month', 'new', 'active', :o, :o, :o)"
            ),
            {"i": cid, "t": tenant_id, "e": email, "o": owner_id},
        )
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer,"
                " employer_normalized, title, title_normalized, started_on,"
                " started_precision)"
                " VALUES (:i, :t, :c, :e, :e, :title, :title, '2022-01-15', 'month')"
            ),
            {
                "i": role_id,
                "t": tenant_id,
                "c": cid,
                "e": employer,
                "title": title,
            },
        )
        for skill in ("cardiac", "emergency"):
            await s.execute(
                text(
                    "INSERT INTO candidate_skills (id, tenant_id, candidate_id, skill,"
                    " skill_normalized) VALUES (:i, :t, :c, :s, :s)"
                ),
                {"i": uuid.uuid4(), "t": tenant_id, "c": cid, "s": skill},
            )
        await s.commit()
    return cid


async def _opportunity(
    tenant_id: uuid.UUID,
    *,
    title: str = "Staff Nurse",
    company: str = "Acme Health",
    skills: list[str] | None = None,
    salary_min: float | None = 2800,
    salary_max: float | None = 3500,
    salary_currency: str = "SGD",
    salary_period: str = "month",
    assigned_to: uuid.UUID | None = None,
    superseded_by: uuid.UUID | None = None,
) -> uuid.UUID:
    oid = uuid.uuid4()
    # `NULL`, never a sentinel like ARRAY['none']: a "none" member would be a
    # real skill the candidate does not hold, and a job order with no skills
    # must make the skills component abstain, not score a miss.
    skills_sql = "ARRAY[" + ", ".join(f"'{s}'" for s in skills) + "]" if skills else "NULL"
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, job_title_raw,"
                " job_title_normalized, skills, company_name_raw,"
                " company_name_normalized, salary_min, salary_max, salary_currency,"
                " salary_period, review_status, quality_state, assigned_user_id,"
                " superseded_by_opportunity_id)"
                " VALUES (:i, :t, :title, :title, "
                + skills_sql
                + ", :company, :company, :mn, :mx, :cur, :per, 'ready', 'likely', :a, :sb)"
            ),
            {
                "i": oid,
                "t": tenant_id,
                "title": title,
                "company": company,
                "mn": salary_min,
                "mx": salary_max,
                "cur": salary_currency,
                "per": salary_period,
                "a": assigned_to,
                "sb": superseded_by,
            },
        )
        await s.commit()
    return oid


async def test_shortlists_top_five_best_first(agency) -> None:
    tid, uid = agency
    cid = await _candidate(tid, uid)
    # The vacancies are chosen so their scores are strictly decreasing with no
    # ties — title match is the heaviest signal, then skills, employer, salary,
    # and a vacancy with nothing comparable ranks last.
    best = await _opportunity(
        tid, title="Staff Nurse", company="Acme Health",
        skills=["cardiac", "emergency"],
    )
    near = await _opportunity(
        tid, title="Senior Staff Nurse", company="Another Clinic",
        salary_min=2800, salary_max=3500,
    )
    mid = await _opportunity(
        tid, title="Staff Nurse", company="Other Clinic",
        salary_min=1800, salary_max=2200,
    )
    far = await _opportunity(
        tid, title="Cleaner", company="Other Clinic",
        salary_min=2800, salary_max=3500,
    )
    low = await _opportunity(
        tid, title="Driver", company="Acme Health", skills=["driving"],
    )
    # One more than the cap, so the response must cut it. No salary on purpose,
    # so it clearly trails the five with salary data.
    extra = await _opportunity(
        tid, title="Clerk", company="Other Clinic", salary_min=None, salary_max=None,
    )

    async with await _http(tid, uid) as http:
        body = (await http.get(f"/api/candidates/{cid}/jobs")).json()

    assert len(body["items"]) == 5
    ids = [row["id"] for row in body["items"]]
    assert ids == [str(best), str(near), str(mid), str(far), str(low)]
    assert str(extra) not in ids

    # Best first: every row's score is a string from a NUMERIC column (the
    # same wire contract sourcing keeps), and scores never rise down the list.
    scores = [row["score"] for row in body["items"]]
    assert all(isinstance(s, str) for s in scores)
    assert scores == sorted(scores, reverse=True)

    # The reasons behind the top score are present and honest: the best fit
    # matched on title, skills and employer, so the breakdown carries data
    # rather than an absent note.
    top = body["items"][0]
    assert top["job_title_raw"] == "Staff Nurse"
    by_name = {r["name"]: r for r in top["reasons"]}
    assert by_name["title"]["raw"] == "1.0000"
    assert by_name["skills"]["raw"] == "1.0000"
    assert by_name["employer"]["raw"] == "1.0000"
    assert body["considered"] == 6
    assert body["scored"] == 6


async def test_a_job_order_with_nothing_comparable_is_not_scored(agency) -> None:
    tid, uid = agency
    # A candidate with no roles, no skills and no salary: nothing on the job
    # order is comparable, so the vacancy must be dropped, never shown as 0%.
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, pipeline_stage,"
                " record_status, owner_id, created_by, updated_by)"
                " VALUES (:i, :t, 'No Data', 'new', 'active', :o, :o, :o)"
            ),
            {"i": cid, "t": tid, "o": uid},
        )
        await s.commit()

    await _opportunity(tid, title="Staff Nurse", company="Acme Health")
    await _opportunity(tid, title="Driver", company="Other Clinic")

    async with await _http(tid, uid) as http:
        body = (await http.get(f"/api/candidates/{cid}/jobs")).json()

    assert body["items"] == []
    assert body["considered"] == 2
    assert body["scored"] == 0


async def test_superseded_revisions_are_excluded(agency) -> None:
    tid, uid = agency
    cid = await _candidate(tid, uid)
    stale = await _opportunity(tid, title="Staff Nurse", company="Acme Health")
    # The stale revision still exists, but a newer email replaced it; only the
    # current revision may be shortlisted.
    await _opportunity(
        tid, title="Staff Nurse", company="Acme Health", superseded_by=stale
    )

    async with await _http(tid, uid) as http:
        body = (await http.get(f"/api/candidates/{cid}/jobs")).json()

    assert [row["id"] for row in body["items"]] == [str(stale)]
    assert body["considered"] == 1


async def test_foreign_candidate_is_404(agency, other_agency) -> None:
    tid, _uid = agency
    other_tid, other_uid = other_agency
    cid = await _candidate(other_tid, other_uid)

    async with await _http(tid, other_uid) as http:
        # The cookie names the OTHER tenant's user, so `cid` belongs to a
        # different agency — 404, never 403, exactly as by-id reads behave.
        res = await http.get(f"/api/candidates/{cid}/jobs")
    assert res.status_code == 404


async def test_a_recruiter_only_sees_what_they_can_see() -> None:
    """A colleague's unshared job order is not considered, nor is a candidate
    they do not hold."""
    tid, viewer = await _seed_tenant("recruiter")
    colleague = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'recruiter')"),
            {"i": colleague, "t": tid, "e": f"u{colleague.hex[:6]}@agency.sg"},
        )
        await s.commit()
    try:
        mine = await _candidate(tid, viewer)
        await _opportunity(
            tid, title="Staff Nurse", company="Acme Health", assigned_to=viewer
        )
        await _opportunity(
            tid, title="Staff Nurse", company="Acme Health", assigned_to=colleague
        )

        async with await _http(tid, viewer) as http:
            body = (await http.get(f"/api/candidates/{mine}/jobs")).json()

        # Only the viewer's own job order was examined — a colleague's book is
        # not this recruiter's to match against.
        assert body["considered"] == 1
        assert len(body["items"]) == 1

        # And the colleague's candidate is a 404 to this viewer, exactly as the
        # by-id candidate route behaves.
        theirs = await _candidate(tid, colleague)
        async with await _http(tid, viewer) as http:
            res = await http.get(f"/api/candidates/{theirs}/jobs")
        assert res.status_code == 404
    finally:
        await _drop_tenant(tid)
