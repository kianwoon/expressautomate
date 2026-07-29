# allow-hardcode: the names, titles and SQL below are test fixture content,
# not an oracle and not configuration.
"""The job that ranks candidates for a job order, and the sweep that finds a
stranded run.

No test here reaches a model: `explain_matches` is monkeypatched at the point
`sourcing_jobs` imported it, so what is exercised is the persistence and the
state machine around the call, which is what this task owns.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.sourcing import SourcingRun
from app.services.sourcing.explain import Explanation, ProtectedReport
from app.services.sourcing.persist import read_matches, serialize_components
from app.services.sourcing.score import Component
from app.workers import sourcing_jobs, tasks
from app.workers.sourcing_jobs import run_sourcing
from tests.conftest import AdminSessionLocal, cleanup_tenant


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        for table in ("sourcing_matches", "sourcing_runs", "candidate_submissions"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.commit()
    await cleanup_tenant(tid)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """Nothing in this module may call a model. The default double explains
    nothing and reports nothing, so a test that cares says so itself."""

    async def _explain(opportunity, candidates, *, codes=(), llm=None):
        return [], ProtectedReport()

    monkeypatch.setattr(sourcing_jobs, "explain_matches", _explain)


async def _opportunity(tenant_id: uuid.UUID, **fields) -> uuid.UUID:
    """A job order and the email it arrived on, since the client link is the
    email rather than a column on the opportunity."""
    mailbox_id, message_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": user_id, "t": tenant_id, "e": f"u{user_id.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, user_id, ms_user_id, folder_id,"
                " scope, retention_months) VALUES (:i, :t, :u, :m, 'inbox', 'user', 24)"
            ),
            {"i": mailbox_id, "t": tenant_id, "u": user_id, "m": f"oid-{mailbox_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " internet_message_id, subject)"
                " VALUES (:i, :t, :m, :g, :g, 'Nurse wanted')"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id, "g": message_id.hex},
        )
        columns = {
            "review_status": "ready",
            "quality_state": "likely",
            "job_title_normalized": "staff nurse",
            "company_name_normalized": "acme health",
            "skills": ["triage", "iv therapy"],
            **fields,
        }
        names = ", ".join(columns)
        binds = ", ".join(f":{k}" for k in columns)
        await s.execute(
            text(
                f"INSERT INTO opportunities (id, tenant_id, email_message_id, {names})"
                f" VALUES (:i, :t, :e, {binds})"
            ),
            {"i": opportunity_id, "t": tenant_id, "e": message_id, **columns},
        )
        await s.commit()
    return opportunity_id


async def _candidate(tenant_id: uuid.UUID, *, name: str, title: str, skills=()) -> uuid.UUID:
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, current_title,"
                " record_status, pipeline_stage) VALUES (:i, :t, :n, :c, 'active', 'new')"
            ),
            {"i": cid, "t": tenant_id, "n": name, "c": title},
        )
        for skill in skills:
            await s.execute(
                text(
                    "INSERT INTO candidate_skills (id, tenant_id, candidate_id, skill,"
                    " skill_normalized) VALUES (:i, :t, :c, :s, :s)"
                ),
                {"i": uuid.uuid4(), "t": tenant_id, "c": cid, "s": skill},
            )
        await s.commit()
    return cid


async def _run(tenant_id: uuid.UUID, opportunity_id: uuid.UUID, **fields) -> uuid.UUID:
    rid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO sourcing_runs (id, tenant_id, opportunity_id, state, attempts)"
                " VALUES (:i, :t, :o, :s, :a)"
            ),
            {
                "i": rid,
                "t": tenant_id,
                "o": opportunity_id,
                "s": fields.get("state", SourcingRun.PENDING),
                "a": fields.get("attempts", 0),
            },
        )
        await s.commit()
    return rid


async def _row(run_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT state, attempts, candidates_considered, shortlisted,"
                    " protected_attribute_noticed, protected_attribute_note"
                    " FROM sourcing_runs WHERE id = :i"
                ),
                {"i": run_id},
            )
        ).one()


async def _matches(tenant_id: uuid.UUID, run_id: uuid.UUID):
    async with tenant_session(tenant_id) as s:
        return await read_matches(s, tenant_id=tenant_id, run_id=run_id)


def test_a_component_survives_the_trip_through_jsonb():
    """`Decimal` is not JSON-serialisable, so a component written verbatim
    throws at insert. This is the guard on that."""
    import json

    serialised = serialize_components(
        [
            Component(name="skills", weight=Decimal("3"), raw=Decimal("0.5"),
                      contribution=Decimal("1.5"), note="Matched 1 of 2."),
            Component(name="salary", weight=Decimal("2"), raw=None,
                      contribution=None, note="No comparable salary."),
        ]
    )
    assert json.loads(json.dumps(serialised)) == serialised
    # Absent stays absent: `None` must not become a zero on the way through.
    assert serialised[1]["raw"] is None
    assert serialised[0]["raw"] == "0.5"


async def test_a_run_stores_its_matches_with_scores_and_reasons(agency):
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    strong = await _candidate(
        tenant_id, name="Jane Tan", title="staff nurse", skills=("triage", "iv therapy")
    )
    weak = await _candidate(tenant_id, name="Bob Lee", title="chef", skills=("baking",))
    run_id = await _run(tenant_id, opportunity_id)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    row = await _row(run_id)
    assert row.state == SourcingRun.DONE
    assert row.candidates_considered == 2

    matches = await _matches(tenant_id, run_id)
    assert [m.candidate_id for m in matches] == [strong, weak]
    assert matches[0].score > matches[1].score
    # Four decimal places survive the column: at two, distinct scores would
    # collapse into ties and the ranking would wobble between readings.
    assert matches[0].score == matches[0].score.quantize(Decimal("0.0001"))
    names = {component["name"] for component in matches[0].reasons}
    assert "skills" in names and "title" in names


async def test_a_candidate_nothing_can_be_compared_for_is_dropped(agency):
    """`score_candidate` returns None and the column is NOT NULL. Storing a
    zero instead would say "unsuitable" about somebody we know nothing of."""
    tenant_id = agency
    # A job order with nothing comparable in it at all.
    opportunity_id = await _opportunity(
        tenant_id, job_title_normalized=None, company_name_normalized=None, skills=None
    )
    await _candidate(tenant_id, name="Jane Tan", title="staff nurse")
    run_id = await _run(tenant_id, opportunity_id)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    row = await _row(run_id)
    assert row.state == SourcingRun.DONE
    assert row.candidates_considered == 1
    assert await _matches(tenant_id, run_id) == []


async def test_a_stored_run_does_not_change_when_the_candidate_does(agency):
    """The whole reason a run is a record rather than a live query."""
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    candidate_id = await _candidate(
        tenant_id, name="Jane Tan", title="staff nurse", skills=("triage",)
    )
    run_id = await _run(tenant_id, opportunity_id)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )
    before = (await _matches(tenant_id, run_id))[0]
    original_score, original_reasons = before.score, before.reasons

    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE candidates SET current_title = 'chef' WHERE id = :i"),
            {"i": candidate_id},
        )
        await s.execute(
            text("DELETE FROM candidate_skills WHERE candidate_id = :i"),
            {"i": candidate_id},
        )
        await s.commit()

    after = (await _matches(tenant_id, run_id))[0]
    assert after.score == original_score
    assert after.reasons == original_reasons


async def test_the_protected_report_lands_on_the_run(agency, monkeypatch):
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    await _candidate(tenant_id, name="Jane Tan", title="staff nurse", skills=("triage",))
    run_id = await _run(tenant_id, opportunity_id)

    async def _explain(opportunity, candidates, *, codes=(), llm=None):
        return [], ProtectedReport(
            noticed=True,
            requirements=["Chinese speaking only"],
            redacted_codes=["C/F"],
        )

    monkeypatch.setattr(sourcing_jobs, "explain_matches", _explain)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    row = await _row(run_id)
    assert row.protected_attribute_noticed is True
    assert "Chinese speaking only" in row.protected_attribute_note
    assert "C/F" in row.protected_attribute_note


async def test_an_explanation_is_stored_beside_the_candidate_it_is_about(
    agency, monkeypatch
):
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    candidate_id = await _candidate(
        tenant_id, name="Jane Tan", title="staff nurse", skills=("triage",)
    )
    run_id = await _run(tenant_id, opportunity_id)

    async def _explain(opportunity, candidates, *, codes=(), llm=None):
        return [
            Explanation(
                candidate_id=candidate_id,
                reason="Six years on a triage desk.",
                evidence="triage desk",
                confidence=0.9,
            )
        ], ProtectedReport()

    monkeypatch.setattr(sourcing_jobs, "explain_matches", _explain)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    match = (await _matches(tenant_id, run_id))[0]
    assert match.explanation == "Six years on a triage desk."
    assert match.explanation_evidence == "triage desk"


async def test_a_run_already_answered_is_left_alone(agency):
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    await _candidate(tenant_id, name="Jane Tan", title="staff nurse")
    run_id = await _run(tenant_id, opportunity_id, state=SourcingRun.DONE)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    row = await _row(run_id)
    assert row.state == SourcingRun.DONE
    # Not touched at all: an answered run must not spend an attempt either.
    assert row.attempts == 0
    assert await _matches(tenant_id, run_id) == []


async def test_a_repeatedly_failing_run_reaches_failed_and_stops(agency, monkeypatch):
    """`rescan_stuck` hands a non-terminal run straight back, so without the
    attempt bound a job order that crashes the scorer loops for ever."""
    monkeypatch.setattr(settings, "SOURCING_MAX_ATTEMPTS", 2)
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    await _candidate(tenant_id, name="Jane Tan", title="staff nurse")
    run_id = await _run(tenant_id, opportunity_id, attempts=2, state=SourcingRun.RUNNING)

    await run_sourcing(
        None,
        tenant_id=str(tenant_id),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    row = await _row(run_id)
    assert row.state == SourcingRun.FAILED
    assert row.attempts == 3


async def test_a_stranded_run_is_re_enqueued(agency, monkeypatch):
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    run_id = await _run(tenant_id, opportunity_id, state=SourcingRun.RUNNING)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE sourcing_runs SET updated_at = now() - interval '1 day'"
                " WHERE id = :i"
            ),
            {"i": run_id},
        )
        await s.commit()

    queued: list[tuple] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)

    assert await tasks.rescan_stuck() >= 1
    mine = [k for name, k in queued if name == "run_sourcing" and k["run_id"] == str(run_id)]
    assert mine == [
        {
            "tenant_id": str(tenant_id),
            "opportunity_id": str(opportunity_id),
            "run_id": str(run_id),
        }
    ]


async def test_a_run_for_another_tenant_does_nothing(agency):
    """RLS decides, and a job naming a mismatched pair must read no row rather
    than reach across the boundary."""
    tenant_id = agency
    opportunity_id = await _opportunity(tenant_id)
    run_id = await _run(tenant_id, opportunity_id)

    await run_sourcing(
        None,
        tenant_id=str(uuid.uuid4()),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
    )

    row = await _row(run_id)
    assert row.state == SourcingRun.PENDING
    assert row.attempts == 0
