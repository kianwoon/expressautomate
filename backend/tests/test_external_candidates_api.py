"""The External Candidates API — the career bot proxy.

Mirrors `test_job_intelligence_api.py` in shape: seed an agency and a job
order through the admin role, sign in with the cookie the OAuth callback
would have set, and drive the routes over ASGI transport. The career bot is
never reached — `CareerBotClient`'s three calls are replaced per test, the
same way `enqueue` is replaced in the intelligence tests. No test here
touches a network.

The adversarial assertions are the boundary ones:
- another agency's job order is 404 on every route;
- a task id no local row vouches for is 404, even though the career bot
  would have answered it — the membership check, not the remote service,
  decides who may read a search;
- a job order with no search plan refuses with 409 naming the Search tab;
- an unreachable / refusing career bot is a structured status, never a 500
  (CLAUDE.md's rule for every external system).

allow-hardcode: the SQL, model ids and responses below are test fixtures.
"""

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.main import app
from app.services import career_bot
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


async def _seed_agency() -> tuple[uuid.UUID, uuid.UUID]:
    tid, uid = uuid.uuid4(), uuid.uuid4()
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
        await s.commit()
    return tid, uid


async def _drop_agency(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in (
            "external_candidate_searches",
            "job_intelligence",
            "opportunities",
            "email_messages",
            "mailboxes",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.commit()
    await cleanup_tenant(tid)


async def _opportunity(tenant_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    mailbox_id, message_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
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
                " VALUES (:i, :t, :m, :g, :g, 'Role wanted')"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id, "g": message_id.hex},
        )
        await s.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_raw, job_description, review_status, quality_state)"
                " VALUES (:i, :t, :e, 'Logistics Manager', 'Run the warehouse.',"
                " 'ready', 'likely')"
            ),
            {"i": opportunity_id, "t": tenant_id, "e": message_id},
        )
        await s.commit()
    return opportunity_id


async def _analyse(
    tenant_id: uuid.UUID, opportunity_id: uuid.UUID, plan: dict | None = None
) -> None:
    """A finished job_intelligence row carrying a search plan — the state
    Run Analysis leaves behind, written directly rather than by driving the
    queue (the analysis itself is `test_job_intelligence_api.py`'s subject).
    `CAST(:p AS jsonb)`, not `:p::jsonb`: asyncpg reserves the `::` form
    inside its own named-parameter translation and a bare cast trips it.
    """
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO job_intelligence (id, tenant_id, opportunity_id,"
                " state, search_plan)"
                " VALUES (:i, :t, :o, 'done', CAST(:p AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "t": tenant_id,
                "o": opportunity_id,
                "p": json.dumps(plan) if plan is not None else None,
            },
        )
        await s.commit()


def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, uid, tid)
    return client


PLAN = {
    "platform": "linkedin",
    "queries": ["Logistics AND Manager", "warehouse AND Singapore"],
    "negative_queries": ["intern"],
    "salary": "SGD 6k-8k",
    "location": "Singapore",
    "employment_type": "Full-time",
}


@pytest.fixture
def configured(monkeypatch):
    """The integration is on, with a fake career bot answering in `calls`.

    Replacing the client's methods (not httpx itself) keeps the tests honest
    about which seam the routes actually use — the service module is the
    only thing the routes know.
    """
    monkeypatch.setattr(settings, "CAREER_BOT_URL", "http://career-bot.test")
    monkeypatch.setattr(settings, "CAREER_BOT_API_KEY", "test-key")
    calls: list[tuple[str, str, dict | None]] = []
    task_ids = iter([f"task-{n}" for n in range(1000)])

    async def _start(self, payload):
        calls.append(("start", json.dumps(payload, sort_keys=True), None))
        return career_bot.StartedSearch(task_id=next(task_ids))

    async def _get_task(self, task_id):
        calls.append(("task", task_id, None))
        return career_bot.TaskStatus(status="running", error=None)

    async def _get_results(self, task_id):
        calls.append(("results", task_id, None))
        return {
            "task_id": task_id,
            "status": "completed",
            "summary": "2 ranked results",
            "results": [
                {"id": "r1", "title": "One", "match_score": 90.0},
                {"id": "r2", "title": "Two", "match_score": 80.0},
            ],
        }

    monkeypatch.setattr(career_bot.CareerBotClient, "start_search", _start)
    monkeypatch.setattr(career_bot.CareerBotClient, "get_task", _get_task)
    monkeypatch.setattr(career_bot.CareerBotClient, "get_results", _get_results)
    return calls


def _payload_of(calls, index: int = 0) -> dict:
    return json.loads(calls[index][1])


async def test_post_maps_the_search_plan_and_returns_a_task(configured):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["status"] == "started"
            assert body["task_id"]

            # The plan maps onto the career bot's body: queries as-is,
            # negative_queries as `exclude`, the rest travelling by name.
            payload = _payload_of(configured)
            assert payload["queries"] == PLAN["queries"]
            assert payload["exclude"] == ["intern"]
            assert payload["platform"] == "linkedin"
            assert payload["location"] == "Singapore"
            assert payload["salary"] == "SGD 6k-8k"
            assert payload["employment_type"] == "Full-time"

            # The membership row exists, on the opportunity the plan came from.
            async with AdminSessionLocal() as s:
                rows = (
                    await s.execute(
                        text(
                            "SELECT task_id, opportunity_id FROM external_candidate_searches"
                            " WHERE tenant_id = :t"
                        ),
                        {"t": tid},
                    )
                ).fetchall()
            assert [tuple(r) for r in rows] == [(body["task_id"], oid)]
    finally:
        await _drop_agency(tid)


async def test_post_without_a_search_plan_is_409(configured):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 409, res.text
            assert "Search tab" in res.json()["detail"]
    finally:
        await _drop_agency(tid)


async def test_post_with_a_plan_of_no_usable_queries_is_409(configured):
    """A plan whose queries are all blank is as good as no plan."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, {"queries": ["  ", ""], "location": "Singapore"})
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 409, res.text
    finally:
        await _drop_agency(tid)


async def test_another_agencys_job_order_is_404(configured):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    other_tid, other_uid = await _seed_agency()
    try:
        async with _http(other_tid, other_uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 404
            got = await c.get(
                f"/api/opportunities/{oid}/external-candidates/search/task-0"
            )
            assert got.status_code == 404
    finally:
        await _drop_agency(tid)
        await _drop_agency(other_tid)


async def test_a_task_id_without_a_local_row_is_404(configured):
    """The membership check, not the career bot, decides: a task id the
    fake career bot would happily answer is refused because no row of ours
    vouches for it — the other agency's id must read exactly like a
    fabricated one."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            got = await c.get(
                f"/api/opportunities/{oid}/external-candidates/search/not-a-task"
            )
            assert got.status_code == 404
            got = await c.get(
                f"/api/opportunities/{oid}/external-candidates/search/not-a-task/results"
            )
            assert got.status_code == 404
        assert configured == []  # the career bot was never asked
    finally:
        await _drop_agency(tid)


async def test_status_and_results_pass_through_for_the_owning_tenant(configured):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            started = await c.post(
                f"/api/opportunities/{oid}/external-candidates/search"
            )
            task_id = started.json()["task_id"]

            poll = await c.get(
                f"/api/opportunities/{oid}/external-candidates/search/{task_id}"
            )
            assert poll.status_code == 200
            assert poll.json()["task_status"] == "running"

            done = await c.get(
                f"/api/opportunities/{oid}/external-candidates/search/{task_id}/results"
            )
            assert done.status_code == 200
            body = done.json()
            assert body["status"] == "ok"
            assert body["task_status"] == "completed"
            assert [r["title"] for r in body["results"]] == ["One", "Two"]
        assert ("task", task_id, None) in configured
        assert ("results", task_id, None) in configured
    finally:
        await _drop_agency(tid)


async def test_another_agencys_visible_task_id_is_404(configured):
    """The task id leaks, the opportunity is foreign: the membership row
    lives in the owner's tenant, RLS hides it, and the read is 404 before
    the career bot is ever asked."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    other_tid, other_uid = await _seed_agency()
    other_oid = await _opportunity(other_tid, other_uid)
    await _analyse(other_tid, other_oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            started = await c.post(
                f"/api/opportunities/{oid}/external-candidates/search"
            )
            task_id = started.json()["task_id"]
        async with _http(other_tid, other_uid) as c:
            got = await c.get(
                f"/api/opportunities/{other_oid}/external-candidates/search/{task_id}/results"
            )
            assert got.status_code == 404
        assert ("results", task_id, None) not in configured
    finally:
        await _drop_agency(tid)
        await _drop_agency(other_tid)


async def test_a_dead_career_bot_is_a_structured_answer_never_a_500(monkeypatch):
    monkeypatch.setattr(settings, "CAREER_BOT_URL", "http://career-bot.test")
    monkeypatch.setattr(settings, "CAREER_BOT_API_KEY", "test-key")

    async def _dead(self, payload):
        raise career_bot.CareerBotUnreachableError(
            "the external candidate search service could not be reached"
        )

    monkeypatch.setattr(career_bot.CareerBotClient, "start_search", _dead)
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["status"] == "unreachable"
            assert "could not be reached" in body["message"]
            # Nothing was recorded: a task id we could not start has no row.
            async with AdminSessionLocal() as s:
                n = (
                    await s.execute(
                        text(
                            "SELECT count(*) FROM external_candidate_searches"
                            " WHERE tenant_id = :t"
                        ),
                        {"t": tid},
                    )
                ).scalar_one()
            assert n == 0
    finally:
        await _drop_agency(tid)


async def test_a_refusing_career_bot_quotes_its_reason(monkeypatch):
    monkeypatch.setattr(settings, "CAREER_BOT_URL", "http://career-bot.test")
    monkeypatch.setattr(settings, "CAREER_BOT_API_KEY", "test-key")

    async def _refuse(self, payload):
        raise career_bot.CareerBotError("unsupported platform 'xing'", 422)

    monkeypatch.setattr(career_bot.CareerBotClient, "start_search", _refuse)
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["status"] == "refused"
            assert body["message"] == "unsupported platform 'xing'"
    finally:
        await _drop_agency(tid)


async def test_a_missing_membership_table_is_structured_never_a_500(monkeypatch):
    """2026-09-02 in production: api shipped the feature's code before its
    migration ran, and `INSERT INTO external_candidate_searches` surfaced as
    a 500 with a 120-line stack trace. A missing schema is a deployment
    fault — it gets the same structured-answer treatment as an unreachable
    career bot, with a sentence an operator can act on.

    Simulates the fault at the real seam: the INSERT the route runs, not a
    mock of the ORM.
    """
    monkeypatch.setattr(settings, "CAREER_BOT_URL", "http://career-bot.test")
    monkeypatch.setattr(settings, "CAREER_BOT_API_KEY", "test-key")

    async def _start_ok(self, payload):
        return career_bot.StartedSearch(task_id="task-missing-table")

    monkeypatch.setattr(career_bot.CareerBotClient, "start_search", _start_ok)

    class _MissingTable:
        sqlstate = "42P01"

        def __str__(self):
            return 'relation "external_candidate_searches" does not exist'

    async def _insert_fails(self, *args, **kwargs):
        raise DBAPIError(
            "INSERT INTO external_candidate_searches", {}, _MissingTable()
        )

    # Patch the route module's session factory so ONLY the request's session
    # fails to commit — the fixtures seed and clean up through the admin
    # session, and patching AsyncSession.commit globally would break those.
    # auth's `_require_session_with_role` opens its own tenant session before
    # the route's, so the fault fires only on the SECOND commit through a
    # patched factory (auth's is the first): the route's INSERT commit.
    import app.db.rls as rls_module

    real_session_local = rls_module.SessionLocal
    armed = {"fired": False}

    class _BrokenSessionLocal:
        def __call__(self):
            session = real_session_local()

            async def _commit(*a, **kw):
                if armed["fired"] and session.in_transaction():
                    # Fail only a commit that still carries work — a
                    # post-rollback no-op commit passes, matching the real
                    # semantics the route relies on (`expunge_all` +
                    # `rollback` leave the exit commit empty).
                    raise DBAPIError(
                        "INSERT INTO external_candidate_searches",
                        {},
                        _MissingTable(),
                    )
                armed["fired"] = True
                return await session.__class__.commit(session)

            session.commit = _commit
            return session

    monkeypatch.setattr(rls_module, "SessionLocal", _BrokenSessionLocal())
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["status"] == "not_provisioned"
            assert "migrations" in body["message"]
            assert body["task_id"] is None
    finally:
        await _drop_agency(tid)


async def test_an_unconfigured_integration_is_answerable(monkeypatch):
    """The GRAPH_BASE_URL/R2_* precedent: an unset URL/key is a deployment
    state, answered at the edge rather than crashed on."""
    monkeypatch.setattr(settings, "CAREER_BOT_URL", "")
    monkeypatch.setattr(settings, "CAREER_BOT_API_KEY", "")
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    await _analyse(tid, oid, PLAN)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(f"/api/opportunities/{oid}/external-candidates/search")
            assert res.status_code == 202, res.text
            assert res.json()["status"] == "unconfigured"
    finally:
        await _drop_agency(tid)


async def test_the_career_bot_client_sends_the_key_and_never_leaks_it(monkeypatch):
    """The transport contract: `X-API-Key` on the request, the key and URL
    in nothing the caller can see when the service is dead."""
    monkeypatch.setattr(settings, "CAREER_BOT_URL", "http://career-bot.test")
    monkeypatch.setattr(settings, "CAREER_BOT_API_KEY", "test-key-abc")

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-API-Key")
        seen["url"] = str(request.url)
        return httpx.Response(500, json={"error": {"message": "boom", "status": 500}})

    async with AsyncClient(
        transport=httpx.MockTransport(_handler), base_url=settings.CAREER_BOT_URL
    ) as http:
        client = career_bot.CareerBotClient(client=http)

        try:
            await client.start_search({"queries": ["x"]})
        except career_bot.CareerBotUnreachableError as exc:
            # Neither the URL nor the key may surface in what a caller
            # could log or serialise.
            assert "career-bot" not in str(exc)
            assert "test-key-abc" not in str(exc)
            assert "test-key-abc" not in exc.message
        else:  # pragma: no cover
            raise AssertionError("a 500 must raise CareerBotUnreachableError")

    assert seen["key"] == "test-key-abc"
    assert seen["url"].startswith("http://career-bot.test/api/v1/search/candidates")


async def test_the_error_envelope_message_is_carried_verbatim():
    """§15: the reason we were given is the reason we show, bounded."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "validation_error",
                    "message": "Provide `queries` (list) or `query` (string)",
                    "status": 422,
                }
            },
        )

    async with AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://career-bot.test"
    ) as http:
        client = career_bot.CareerBotClient(client=http)
        with pytest.raises(career_bot.CareerBotError) as excinfo:
            await client.start_search({})
    assert excinfo.value.message == "Provide `queries` (list) or `query` (string)"
    assert excinfo.value.status == 422
