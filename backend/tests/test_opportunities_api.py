"""The job-order list a recruiter reads (plan §16, §17).

Three things this endpoint must never get wrong, one of which is a security
property:

- Agency A must not see Agency B's vacancies. The list is the product; a leak
  here hands a competitor's live roles to a competitor.
- Newest first, because the received date is the column the spreadsheet this
  replaces never had, and an unsorted list makes it decorative.
- A field the email did not mention stays null. Substituting "" or 0 makes an
  absence indistinguishable from an extracted value, which is the fabrication
  §15 forbids — and the UI renders it as data.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.db.rls import tenant_session
from tests.conftest import AdminSessionLocal, sign_in

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def settings_the_suite_supplies(monkeypatch) -> None:
    """CI has no `.env`, so the suite states every value it depends on.

    Unconditional rather than a fallback: relying on the developer machine's
    `.env` for the limit would test a different number locally than in CI.
    """
    monkeypatch.setattr(settings, "OPPORTUNITIES_PAGE_LIMIT", 200)


async def test_one_agency_never_sees_another_agencys_vacancies(client, seeded) -> None:
    """The security property. Everything else on this page is cosmetic beside it."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_a, user_a, mailbox_a = await make_tenant("agency-a")
    tenant_b, _user_b, mailbox_b = await make_tenant("agency-b")
    await make_opportunity(tenant_a, mailbox_a, company_name_raw="Acme Pte Ltd")
    await make_opportunity(tenant_b, mailbox_b, company_name_raw="Rival Holdings")

    sign_in(client, user_a, tenant_a)
    body = (await client.get("/api/opportunities")).json()

    companies = [row["company_name_raw"] for row in body["items"]]
    assert companies == ["Acme Pte Ltd"]
    assert "Rival Holdings" not in companies


async def test_newest_first(client, seeded) -> None:
    """The received date is the column the spreadsheet lacked; ordering is what
    makes it worth having."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Older", received_datetime=NOW - timedelta(days=5)
    )
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Newest", received_datetime=NOW)
    await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Middle", received_datetime=NOW - timedelta(days=1)
    )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    assert [row["company_name_raw"] for row in body["items"]] == [
        "Newest",
        "Middle",
        "Older",
    ]


async def test_a_field_the_email_did_not_mention_stays_null(client, seeded) -> None:
    """No empty strings, no zeros. The UI reads null as "Not mentioned" (§15);
    a substituted value would be rendered as though the email said it."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd")

    sign_in(client, user_id, tenant_id)
    row = (await client.get("/api/opportunities")).json()["items"][0]

    for field in (
        "salary_raw",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "working_hours_raw",
        "requirements",
        "duration_raw",
        "location_raw",
        "job_title_raw",
        "job_description",
    ):
        assert row[field] is None, f"{field} was filled in with {row[field]!r}"


async def test_the_job_description_reaches_the_screen(client, seeded) -> None:
    """The table has a column for it, so the payload has to carry it.

    Asserted separately from the null case because the two failures look
    nothing alike: a missing key 404s the column for every row, while a
    substituted "" would render as a job with no description written.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme Pte Ltd",
        job_description="Manage the front desk and greet visitors.",
    )

    sign_in(client, user_id, tenant_id)
    row = (await client.get("/api/opportunities")).json()["items"][0]

    assert row["job_description"] == "Manage the front desk and greet visitors."


async def test_an_anonymous_caller_gets_nothing(client) -> None:
    """No session, no list — the guard is the endpoint's, not the browser's."""
    assert (await client.get("/api/opportunities")).status_code == 401


async def test_a_page_reports_the_total_it_is_a_page_of(client, seeded) -> None:
    """`total` is the size of the result set, not of the page.

    Without it the table has no way to render "showing 2 of 5" or to know a
    next page exists, and paging degrades into clicking until a page comes
    back empty.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    for n in range(5):
        await make_opportunity(
            tenant_id,
            mailbox_id,
            company_name_raw=f"Co {n}",
            received_datetime=NOW - timedelta(days=n),
        )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?limit=2&offset=1")).json()

    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    # Offset 1 into a newest-first list skips "Co 0"; the window is stable
    # because the ordering breaks ties on id.
    assert [row["company_name_raw"] for row in body["items"]] == ["Co 1", "Co 2"]


async def test_the_limit_cannot_exceed_the_configured_ceiling(client, seeded) -> None:
    """The page size is an operational fact, not something a caller may raise."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd")

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?limit=100000")).json()

    assert body["limit"] == settings.OPPORTUNITIES_PAGE_LIMIT


async def test_the_counts_are_tenant_wide_not_page_wide(client, seeded) -> None:
    """The single reason `counts` is computed in its own query.

    A chip reading "3 need review" that becomes "1 needs review" when the user
    pages is not a smaller truth — it is silently answering a different
    question, and the recruiter's decision about what to work next is made on
    the wrong number.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    for n in range(4):
        await make_opportunity(
            tenant_id,
            mailbox_id,
            company_name_raw=f"New {n}",
            received_datetime=NOW - timedelta(days=n),
        )
    for n in range(3):
        await make_opportunity(
            tenant_id,
            mailbox_id,
            company_name_raw=f"Doubtful {n}",
            review_status="needs_review",
            received_datetime=NOW - timedelta(days=10 + n),
        )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?limit=1")).json()

    assert len(body["items"]) == 1
    assert body["counts"] == {"all": 7, "new": 4, "needs_review": 3, "reviewed": 0}


async def test_the_counts_ignore_the_status_filter(client, seeded) -> None:
    """Filtering to one chip must not zero the others.

    The chips are the control that clears the filter; if selecting `new` made
    `needs_review` read 0, the user would have no visible way back.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Fresh")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Doubtful",
                           review_status="needs_review")

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?status=new")).json()

    assert [row["company_name_raw"] for row in body["items"]] == ["Fresh"]
    assert body["total"] == 1
    assert body["counts"] == {"all": 2, "new": 1, "needs_review": 1, "reviewed": 0}


async def test_the_status_filter_selects_one_state(client, seeded) -> None:
    """Each of the three chips, including `new`, which is stored as `ready`.

    `new` is the interesting one: `persist.py` writes `ready` and this API
    renames it, so a caller filtering on the word it was just shown has to
    land on the right rows.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Fresh")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Doubtful",
                           review_status="needs_review")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Done",
                           review_status="reviewed")

    sign_in(client, user_id, tenant_id)
    for status, expected in (("new", "Fresh"), ("needs_review", "Doubtful"), ("reviewed", "Done")):
        body = (await client.get(f"/api/opportunities?status={status}")).json()
        assert [row["company_name_raw"] for row in body["items"]] == [expected], status
        assert [row["review_status"] for row in body["items"]] == [status], status

    # No filter is every state, not a fourth bucket.
    assert (await client.get("/api/opportunities")).json()["total"] == 3


async def test_an_unknown_status_is_rejected_rather_than_silently_ignored(client, seeded) -> None:
    """A typo that returned the unfiltered list would look like a working filter."""
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, user_id, _mailbox_id = await make_tenant("agency-a")

    sign_in(client, user_id, tenant_id)
    assert (await client.get("/api/opportunities?status=ready")).status_code == 422


async def test_the_row_carries_the_ids_that_reopen_the_email(client, seeded) -> None:
    """Both ids, because they address different things.

    `graph_message_id` is per-mailbox and is what a Graph call needs;
    `internet_message_id` is the RFC identifier the same mail carries in every
    recipient's copy. Shipping only one strands whichever caller needs the
    other.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd")

    sign_in(client, user_id, tenant_id)
    row = (await client.get("/api/opportunities")).json()["items"][0]

    assert row["graph_message_id"].startswith("graph-")
    assert row["internet_message_id"].endswith("@example.sg>")


async def test_verified_and_total_fields_come_from_the_evidence_table(client, seeded) -> None:
    """The trust signal the UI shows instead of a confidence percentage."""
    make_tenant, make_opportunity, make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    checked = await make_opportunity(tenant_id, mailbox_id, company_name_raw="Checked")
    await make_evidence(tenant_id, checked, valid=2, invalid=3)
    # A vacancy with no evidence rows at all must read 0 of 0, not crash and
    # not silently inherit another row's numbers.
    await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Bare",
        received_datetime=NOW - timedelta(days=1),
    )

    sign_in(client, user_id, tenant_id)
    items = (await client.get("/api/opportunities")).json()["items"]

    by_company = {row["company_name_raw"]: row for row in items}
    assert (by_company["Checked"]["verified_fields"], by_company["Checked"]["total_fields"]) == (
        2,
        5,
    )
    assert (by_company["Bare"]["verified_fields"], by_company["Bare"]["total_fields"]) == (0, 0)


async def test_model_confidence_never_reaches_a_response(client, seeded) -> None:
    """`models/extraction.py` says it is never shown as a probability.

    Asserted over the serialised body rather than field by field, so a future
    payload that nests it, renames it `confidence`, or returns the raw
    extraction row still trips this.
    """
    make_tenant, make_opportunity, make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd")
    await make_evidence(tenant_id, opportunity_id, valid=1, invalid=1)

    sign_in(client, user_id, tenant_id)
    listing = await client.get("/api/opportunities")
    reviewed = await client.post(
        f"/api/opportunities/{opportunity_id}/review", json={"reviewed": True}
    )

    for response in (listing, reviewed):
        assert "confidence" not in response.text.lower(), response.text
        assert "0.42" not in response.text


async def test_marking_reviewed_and_putting_it_back(client, seeded) -> None:
    """Two-way, and the round trip lands on `new`, not back on `needs_review`.

    Once a human has looked at a row the pipeline's doubt is stale; restoring
    it would return the row to a queue its own reviewer had just cleared.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id,
        mailbox_id,
        company_name_raw="Acme Pte Ltd",
        review_status="needs_review",
        # Assigned, not merely visible: an unassigned job order is claimable
        # but not editable, so the review write is a 403 without this.
        assigned_user_id=user_id,
    )

    sign_in(client, user_id, tenant_id)
    url = f"/api/opportunities/{opportunity_id}/review"

    marked = await client.post(url, json={"reviewed": True})
    assert marked.status_code == 200
    assert marked.json()["review_status"] == "reviewed"
    body = (await client.get("/api/opportunities")).json()
    assert body["items"][0]["review_status"] == "reviewed"
    assert body["counts"] == {"all": 1, "new": 0, "needs_review": 0, "reviewed": 1}

    unmarked = await client.post(url, json={"reviewed": False})
    assert unmarked.json()["review_status"] == "new"
    body = (await client.get("/api/opportunities")).json()
    assert body["items"][0]["review_status"] == "new"
    assert body["counts"] == {"all": 1, "new": 1, "needs_review": 0, "reviewed": 0}


async def test_reviewing_another_agencys_vacancy_is_a_404(client, seeded) -> None:
    """The write side of the isolation property, and it is RLS that supplies it.

    The UPDATE names only the id — no `tenant_id` in the WHERE clause — so a
    403 here would mean the policy had stopped applying to writes. The row must
    also still be untouched afterwards: a 404 returned after the write landed
    would be the worst of both.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_a, user_a, _mailbox_a = await make_tenant("agency-a")
    tenant_b, _user_b, mailbox_b = await make_tenant("agency-b")
    theirs = await make_opportunity(tenant_b, mailbox_b, company_name_raw="Rival Holdings")

    sign_in(client, user_a, tenant_a)
    response = await client.post(f"/api/opportunities/{theirs}/review", json={"reviewed": True})

    assert response.status_code == 404
    async with tenant_session(tenant_b) as s:
        still = (
            await s.execute(
                text("SELECT review_status FROM opportunities WHERE id = :i"), {"i": theirs}
            )
        ).scalar_one()
    assert still != "reviewed"


async def test_reviewing_an_id_that_does_not_exist_is_a_404(client, seeded) -> None:
    """Indistinguishable from another agency's id, deliberately."""
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, user_id, _mailbox_id = await make_tenant("agency-a")

    sign_in(client, user_id, tenant_id)
    response = await client.post(
        f"/api/opportunities/{uuid.uuid4()}/review", json={"reviewed": True}
    )
    assert response.status_code == 404


async def test_an_anonymous_caller_cannot_mark_anything_reviewed(client) -> None:
    """The guard is the endpoint's on the write path too, not only the read."""
    response = await client.post(
        f"/api/opportunities/{uuid.uuid4()}/review", json={"reviewed": True}
    )
    assert response.status_code == 401


# --- server-side search and sort -------------------------------------------------
#
# allow-hardcode: the literal strings below (company names, job titles, search
# terms) are test fixture data — human-authored inputs asserting the API's
# search/sort behaviour — not a detection, scoring, or allow-list oracle.


async def test_salary_sort_normalises_to_monthly_both_directions(client, seeded) -> None:
    """The test that catches a wrong per-period conversion factor.

    5000/month, 30000/year (=2500/month) and 200/day (=200*21.75=4350/month):
    ranked by what the job actually pays, not by the raw figure.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    monthly = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Monthly",
        salary_min=5000, salary_period="month",
    )
    yearly = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Yearly",
        salary_min=30000, salary_period="year",
    )
    daily = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Daily",
        salary_min=200, salary_period="day",
    )

    sign_in(client, user_id, tenant_id)
    asc = (await client.get("/api/opportunities?sort=salary&descending=false")).json()
    desc = (await client.get("/api/opportunities?sort=salary&descending=true")).json()

    # yearly=30000/12=2500, daily=200*21.75=4350, monthly=5000 per month.
    assert [row["id"] for row in asc["items"]] == [str(yearly), str(daily), str(monthly)]
    assert [row["id"] for row in desc["items"]] == [str(monthly), str(daily), str(yearly)]


async def test_the_column_refuses_a_period_no_reader_understands(seeded) -> None:
    """The guarantee the salary sort now relies on, stated by the database.

    The sort matches `salary_period` exactly against its per-period factors, so
    a "Month" in the column would sink a row that should rank. That used to be
    reachable — the extraction's answer went in verbatim — and this is the
    constraint that closed it, rather than a convention the next writer has to
    know about.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, _user_id, mailbox_id = await make_tenant("agency-a")

    # A canonical period is fine, and so is no period at all: an email that
    # names a figure without a period is ordinary, not an error.
    await make_opportunity(tenant_id, mailbox_id, salary_min=5000, salary_period="month")
    await make_opportunity(tenant_id, mailbox_id, salary_min=5000, salary_period=None)

    for refused in ("Month", " month ", "fortnight"):
        with pytest.raises(DBAPIError) as caught:
            await make_opportunity(
                tenant_id, mailbox_id, salary_min=5000, salary_period=refused
            )
        assert "ck_opportunities_salary_period_known" in str(caught.value), refused


async def test_the_column_refuses_a_quality_state_no_reader_understands(seeded) -> None:
    """What the quality sort's rank table is entitled to assume.

    `quality_state()` in ingest/evidence.py was always the only writer and
    always returned one of these three, but nothing said so to the database.
    Now something does, which is why `_quality_rank` can rank exactly three
    states and treat a fourth as impossible rather than as a case to guess at.
    """
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, _user_id, mailbox_id = await make_tenant("agency-a")

    for state in ("needs_review", "likely", "verified"):
        await make_opportunity(tenant_id, mailbox_id, quality_state=state)

    with pytest.raises(DBAPIError) as caught:
        await make_opportunity(tenant_id, mailbox_id, quality_state="speculative")
    assert "ck_opportunities_quality_state_known" in str(caught.value)


async def test_the_quality_sort_puts_the_rows_needing_a_human_first(client, seeded) -> None:
    """Worst first when ascending — the reason anyone sorts this column."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    needs_review = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="NeedsReview", quality_state="needs_review"
    )
    likely = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Likely", quality_state="likely"
    )
    verified = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Verified", quality_state="verified"
    )

    sign_in(client, user_id, tenant_id)
    asc = (await client.get("/api/opportunities?sort=quality&descending=false")).json()
    assert [row["id"] for row in asc["items"]] == [
        str(needs_review), str(likely), str(verified),
    ]


async def test_null_sort_values_sink_in_both_directions(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    priced = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Priced", salary_min=1000, salary_period="month"
    )
    no_period = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="NoPeriod", salary_min=1000, salary_period=None,
        received_datetime=NOW - timedelta(days=1),
    )
    no_amount = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="NoAmount",
        received_datetime=NOW - timedelta(days=2),
    )

    sign_in(client, user_id, tenant_id)
    for descending in ("true", "false"):
        body = (
            await client.get(f"/api/opportunities?sort=salary&descending={descending}")
        ).json()
        ids = [row["id"] for row in body["items"]]
        assert ids[0] == str(priced), descending
        assert set(ids[1:]) == {str(no_period), str(no_amount)}, descending


async def test_received_sort_ascending_still_sinks_null_dates(client, seeded) -> None:
    make_tenant, make_opportunity, make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    # `make_opportunity` always writes `received_datetime` (it defaults to
    # NOW), so the null case is written directly through the admin session.
    dated = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Dated", received_datetime=NOW - timedelta(days=1)
    )
    other = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Undated", received_datetime=NOW
    )
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE opportunities SET received_datetime = NULL WHERE id = :i"),
            {"i": other},
        )
        await s.commit()

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?sort=received&descending=false")).json()

    assert [row["id"] for row in body["items"]] == [str(dated), str(other)]


async def test_quality_sort_matches_the_intended_rank(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    verified = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Verified", quality_state="verified"
    )
    likely = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Likely", quality_state="likely"
    )
    needs_review = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="NeedsReview", quality_state="needs_review"
    )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?sort=quality&descending=false")).json()

    assert [row["id"] for row in body["items"]] == [
        str(needs_review),
        str(likely),
        str(verified),
    ]


async def test_each_text_sort_key_orders_case_insensitively_and_sinks_nulls(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    for key, column in (
        ("company", "company_name_raw"),
        ("position", "job_title_raw"),
        ("hours", "working_hours_raw"),
        ("duration", "duration_raw"),
        ("location", "location_raw"),
    ):
        # A fresh tenant per key: reusing one tenant would leave prior
        # iterations' rows in the list and corrupt the position assertion
        # below, which is not itself under test here.
        tenant_id, user_id, mailbox_id = await make_tenant(f"sort-{key}")

        # `company_name_raw` doubles as a label for the other columns' rows,
        # so when the sorted column *is* company_name_raw, don't also set it
        # for "blank" — that would give it a real value instead of the NULL
        # this assertion needs.
        def row(value, column=column):
            fields = {column: value}
            fields.setdefault("company_name_raw", "Base")
            return fields

        upper = await make_opportunity(tenant_id, mailbox_id, **row("Bravo"))
        lower = await make_opportunity(tenant_id, mailbox_id, **row("alpha"))
        blank_fields = {} if column == "company_name_raw" else {"company_name_raw": "Base"}
        blank = await make_opportunity(tenant_id, mailbox_id, **blank_fields)

        sign_in(client, user_id, tenant_id)
        body = (await client.get(f"/api/opportunities?sort={key}&descending=false")).json()
        ids = {row["id"]: i for i, row in enumerate(body["items"])}
        assert ids[str(lower)] < ids[str(upper)] < ids[str(blank)], key


async def test_search_matches_each_searched_column(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    fields = {
        "company_name_raw": "Acme Recruiting",
        "job_title_raw": "Senior Recruiter",
        "salary_raw": "6k neg.",
        "working_hours_raw": "9 to 6",
        "duration_raw": "Permanent",
        "location_raw": "Raffles Place",
        "requirements": "Must speak Mandarin",
        "job_description": "Handles the full recruitment cycle",
    }
    for field, value in fields.items():
        row_fields = {"company_name_raw": "Distinct Co", field: value}
        oid = await make_opportunity(tenant_id, mailbox_id, **row_fields)
        sign_in(client, user_id, tenant_id)
        body = (await client.get(f"/api/opportunities?q={value.split()[0]}")).json()
        assert str(oid) in [row["id"] for row in body["items"]], field

    salary_row = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Salary Co", salary_min=4200, salary_max=5200
    )
    sign_in(client, user_id, tenant_id)
    for needle in ("4200", "5200"):
        body = (await client.get(f"/api/opportunities?q={needle}")).json()
        assert str(salary_row) in [row["id"] for row in body["items"]]

    # The currency and the period were searchable before, inside the rendered
    # "SGD 5,000 per month" the client built and then searched. Nothing renders
    # that string server-side, so the two columns behind it are searched
    # directly — otherwise a recruiter who has always found a row by typing
    # "SGD" would get nothing and conclude the row was gone.
    priced = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Priced Co",
        salary_min=5000, salary_currency="SGD", salary_period="month",
    )
    sign_in(client, user_id, tenant_id)
    for needle in ("SGD", "sgd", "month"):
        body = (await client.get(f"/api/opportunities?q={needle}")).json()
        assert str(priced) in [row["id"] for row in body["items"]], needle


async def test_search_treats_percent_and_underscore_literally(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme", salary_raw="6k neg.")
    literal = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme", salary_raw="50%_bonus"
    )

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?q=50%25_bonus")).json()

    assert [row["id"] for row in body["items"]] == [str(literal)]


async def test_search_composes_with_status_and_total_reflects_both(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme Reviewed", review_status="reviewed"
    )
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Acme New")
    await make_opportunity(tenant_id, mailbox_id, company_name_raw="Other New")

    sign_in(client, user_id, tenant_id)
    body = (await client.get("/api/opportunities?q=Acme&status=new")).json()

    assert body["total"] == 1
    assert [row["company_name_raw"] for row in body["items"]] == ["Acme New"]


async def test_an_invalid_sort_is_rejected(client, seeded) -> None:
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, user_id, _mailbox_id = await make_tenant("agency-a")

    sign_in(client, user_id, tenant_id)
    assert (await client.get("/api/opportunities?sort=bogus")).status_code == 422


async def test_tenant_isolation_holds_with_q_and_sort_applied(client, seeded) -> None:
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_a, user_a, mailbox_a = await make_tenant("agency-a")
    tenant_b, _user_b, mailbox_b = await make_tenant("agency-b")
    await make_opportunity(tenant_a, mailbox_a, company_name_raw="Acme Pte Ltd")
    await make_opportunity(tenant_b, mailbox_b, company_name_raw="Acme Rival")

    sign_in(client, user_a, tenant_a)
    body = (
        await client.get("/api/opportunities?q=Acme&sort=company&descending=false")
    ).json()

    assert [row["company_name_raw"] for row in body["items"]] == ["Acme Pte Ltd"]
