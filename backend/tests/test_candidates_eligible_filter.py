"""`GET /api/candidates?eligible_for=<opportunity_id>` (plan slice: regulatory
shortlist filtering).

The owner's brief was one word: "only". MOM's permit rules may narrow a
shortlist; nothing else may. This file checks the line, not just the feature:

- A regulatory `not_met` (see `app/services/sourcing/eligibility.py`) removes
  a candidate; `unknown` and `not_applicable` never do — a candidate we cannot
  assess has not been shown ineligible.
- A job's own occupational sex requirement, however genuine and however well
  reasoned, never removes anybody — it is a person's judgement, not a law.
- `race` still does nothing, exactly as before this filter existed.
- No `placement_type` is a 409 with a flat body naming the reason, and the
  list is not filtered at all in that case.
- `excluded_ineligible` matches the number actually removed.
- §18: another tenant's opportunity id is a 404.
"""

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models import Opportunity
from tests.conftest import AdminSessionLocal
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def agency():
    tid, uid, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                " VALUES (:i, :t, :u, :m, 'user', 'inbox', 24)"
            ),
            {"i": mailbox_id, "t": tid, "u": uid, "m": f"oid-{tid.hex[:8]}"},
        )
        await s.commit()
    yield tid, uid, mailbox_id
    async with AdminSessionLocal() as s:
        for table in (
            "candidates",
            "opportunities",
            "email_messages",
            "mailboxes",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def _make_opportunity(tenant_id, mailbox_id, **fields) -> uuid.UUID:
    email_id, opportunity_id = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, internet_message_id,"
                " received_datetime)"
                " VALUES (:i, :t, :m, :g, :n, now())"
            ),
            {
                "i": email_id,
                "t": tenant_id,
                "m": mailbox_id,
                "g": f"graph-{email_id.hex}",
                "n": f"<{email_id.hex}@example.sg>",
            },
        )
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
                email_message_id=email_id,
                job_description="",
                **fields,
            )
        )
        await s.commit()
    return opportunity_id


async def _make_candidate(http: AsyncClient, **fields) -> str:
    fields.setdefault("full_name", f"Candidate {uuid.uuid4().hex[:8]}")
    r = await http.post("/api/candidates", json=fields)
    assert r.status_code == 201, r.text
    return r.json()["id"]


MDW = Opportunity.MDW_WORK_PERMIT

# Inside MOM's permit band (23 to under 50) and the configured education
# minimum, so a candidate built from this dict is regulatory `met` on every
# criterion except whatever the test overrides.
_ELIGIBLE = dict(
    sex="female",
    date_of_birth=date(1995, 1, 1).isoformat(),
    education_years=10,
    nationality="PH",
)


async def test_regulatory_not_met_removes_a_candidate(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id, placement_type=MDW)
        await _make_candidate(http, **_ELIGIBLE)  # female, in range -> kept
        await _make_candidate(http, **{**_ELIGIBLE, "sex": "male"})  # not_met -> removed

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["excluded_ineligible"] == 1
        assert body["scan_truncated"] is False
        assert body["scanned"] == 2


async def test_the_chips_and_letter_bar_go_silent_while_filtering(agency) -> None:
    """`counts` and `initials` describe the unfiltered population, so under
    `eligible_for` they would sit above rows they do not describe — a chip
    reading "All 2" over a single eligible row, and a letter leading to an
    empty page. There is no honest number available (the eligibility pass only
    ever sees the scan window), so the answer is `null` rather than a figure.

    Recomputing them over the eligible set is the tempting fix and is the
    thing this test exists to catch: it would look right in every small
    fixture and be wrong for exactly the agency the scan ceiling was built
    for."""
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id, placement_type=MDW)
        await _make_candidate(http, **_ELIGIBLE)
        await _make_candidate(http, **{**_ELIGIBLE, "sex": "male"})

        filtered = (await http.get(f"/api/candidates?eligible_for={opp_id}")).json()
        assert filtered["counts"] is None
        assert filtered["initials"] is None

        # And the ordinary list still answers both, so this is a filtered-only
        # silence rather than the facets having been dropped altogether.
        plain = (await http.get("/api/candidates")).json()
        assert plain["counts"]["all"] == 2
        assert plain["initials"]


async def test_unknown_does_not_remove_a_candidate(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id, placement_type=MDW)
        # No date_of_birth recorded -> age finding is `unknown`, not `not_met`.
        await _make_candidate(
            http, sex="female", education_years=10, nationality="PH"
        )

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        body = r.json()
        assert body["total"] == 1
        assert body["excluded_ineligible"] == 0


async def test_not_applicable_does_not_remove_a_candidate(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        # A local hire: every MDW criterion is `not_applicable`.
        opp_id = await _make_opportunity(
            tid, mailbox_id, placement_type=Opportunity.LOCAL_HIRE
        )
        await _make_candidate(http, sex="male")  # would fail MDW's sex rule, but it doesn't apply

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        body = r.json()
        assert body["total"] == 1
        assert body["excluded_ineligible"] == 0


async def test_occupational_sex_requirement_never_removes_anybody(agency) -> None:
    """A job's own genuine occupational sex requirement, with a reason set,
    annotates and must never narrow the list — the owner's line, drawn
    explicitly."""
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(
            tid,
            mailbox_id,
            placement_type=Opportunity.LOCAL_HIRE,
            sex_requirement="female",
            sex_requirement_reason="Intimate personal care for a female client.",
        )
        await _make_candidate(http, sex="male")
        await _make_candidate(http, sex="female")

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        body = r.json()
        assert body["total"] == 2
        assert body["excluded_ineligible"] == 0


async def test_no_placement_type_is_409_with_flat_body_and_no_filtering(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id)  # placement_type left NULL
        await _make_candidate(http, sex="male")

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        assert r.status_code == 409
        body = r.json()
        assert body["reason"] == "placement_type_not_set"
        assert "detail" in body
        assert "excluded_ineligible" not in body


async def test_race_query_param_still_does_nothing(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        await _make_candidate(http, race="chinese")
        r = await http.get("/api/candidates?race=chinese")
        assert r.status_code == 200
        assert r.json()["total"] == 1


async def test_another_tenants_opportunity_id_is_404(agency) -> None:
    tid, uid, mailbox_id = agency
    other_tid, other_mailbox = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": other_tid, "n": f"other-{other_tid.hex[:6]}"},
        )
        other_uid = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": other_uid, "t": other_tid, "e": f"u{other_uid.hex[:6]}@other.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                " VALUES (:i, :t, :u, :m, 'user', 'inbox', 24)"
            ),
            {"i": other_mailbox, "t": other_tid, "u": other_uid, "m": f"oid-{other_tid.hex[:8]}"},
        )
        await s.commit()
    try:
        other_opp_id = await _make_opportunity(other_tid, other_mailbox, placement_type=MDW)
        async with await _client_for(tid, uid) as http:
            await _make_candidate(http, sex="male")
            r = await http.get(f"/api/candidates?eligible_for={other_opp_id}")
            assert r.status_code == 404
    finally:
        async with AdminSessionLocal() as s:
            for table in ("candidates", "opportunities", "email_messages", "mailboxes", "users"):
                await s.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": other_tid}
                )
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tid})
            await s.commit()


async def test_placement_type_records_who_set_it_and_when(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id)
        r = await http.post(
            f"/api/opportunities/{opp_id}/placement-type",
            json={"placement_type": "mdw_work_permit"},
        )
        assert r.status_code == 200

    async with tenant_session(tid) as s:
        row = (
            await s.execute(
                text(
                    "SELECT placement_type_set_by, placement_type_set_at "
                    "FROM opportunities WHERE id = :i"
                ),
                {"i": opp_id},
            )
        ).one()
    assert row.placement_type_set_by == uid
    assert row.placement_type_set_at is not None


async def test_occupational_requirement_records_who_set_it_and_when(agency) -> None:
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id)
        r = await http.post(
            f"/api/opportunities/{opp_id}/occupational-requirement",
            json={"sex_requirement": "female", "sex_requirement_reason": "Intimate care."},
        )
        assert r.status_code == 200

    async with tenant_session(tid) as s:
        row = (
            await s.execute(
                text(
                    "SELECT sex_requirement_set_by, sex_requirement_set_at "
                    "FROM opportunities WHERE id = :i"
                ),
                {"i": opp_id},
            )
        ).one()
    assert row.sex_requirement_set_by == uid
    assert row.sex_requirement_set_at is not None


# --- the scan ceiling -------------------------------------------------------
#
# `all_rows = base.order_by(order)...all()` used to fetch every matching
# candidate in the tenant on every `?eligible_for=` request — unbounded, and
# on a large agency's list, expensive enough to matter on the modest instance
# this backend runs on. These tests pin the ceiling low with `monkeypatch` so
# a handful of candidates is enough to exercise it, rather than needing
# thousands of rows to prove the limit is real.


async def test_scan_under_ceiling_reports_no_truncation(agency, monkeypatch) -> None:
    monkeypatch.setattr(settings, "CANDIDATES_ELIGIBILITY_SCAN_LIMIT", 5)
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id, placement_type=MDW)
        for _ in range(3):
            await _make_candidate(http, **_ELIGIBLE)

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        body = r.json()
        assert body["scan_truncated"] is False
        assert body["scanned"] == 3
        assert body["total"] == 3
        assert body["excluded_ineligible"] == 0


async def test_scan_over_ceiling_reports_truncation_and_still_filters(agency, monkeypatch) -> None:
    monkeypatch.setattr(settings, "CANDIDATES_ELIGIBILITY_SCAN_LIMIT", 3)
    tid, uid, mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        opp_id = await _make_opportunity(tid, mailbox_id, placement_type=MDW)
        # 5 candidates against a ceiling of 3 — the scan must stop at 3, and
        # the rows it did look at must still be correctly filtered. The exact
        # membership of the scanned window is an ordering detail this test
        # does not pin down; what must hold regardless is that the scan
        # stopped at the ceiling and that filtering was applied to exactly
        # what was scanned.
        for _ in range(2):
            await _make_candidate(http, **{**_ELIGIBLE, "sex": "male"})
        for _ in range(3):
            await _make_candidate(http, **_ELIGIBLE)

        r = await http.get(f"/api/candidates?eligible_for={opp_id}")
        body = r.json()
        assert body["scan_truncated"] is True
        assert body["scanned"] == 3
        assert body["total"] + body["excluded_ineligible"] == body["scanned"]
        assert body["total"] <= 3


async def test_unfiltered_listing_never_truncates_or_scans(agency, monkeypatch) -> None:
    """The plain list keeps paging in SQL — the ceiling exists only on the
    `?eligible_for=` path, and must not leak into the ordinary response
    shape."""
    monkeypatch.setattr(settings, "CANDIDATES_ELIGIBILITY_SCAN_LIMIT", 1)
    tid, uid, _mailbox_id = agency
    async with await _client_for(tid, uid) as http:
        for _ in range(3):
            await _make_candidate(http, **_ELIGIBLE)

        r = await http.get("/api/candidates")
        body = r.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        assert "scan_truncated" not in body
        assert "scanned" not in body
        assert "excluded_ineligible" not in body
