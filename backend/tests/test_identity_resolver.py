"""The Candidate Identity Resolver — scoring, early stop, and the API cache.

Two layers, deliberately:

- **Scoring/resolution** is unit-tested against an injected fake provider
  (`WebSearchProvider` is the seam, spec §10), so no test here makes a network
  call. These assert the §15 weights, the §16 thresholds, the §17 safety gate
  and the §32 early stop.
- **The API** is driven over ASGI transport like `test_external_candidates_
  api.py`, with the provider substituted via `app.dependency_overrides`. It
  asserts the §34 cache: a second click on the same fingerprint returns the
  stored row without a second provider call.

The adversarial assertions are the boundary ones:
- exact-name-only evidence is NEVER `resolved` (§52);
- a contradicting page blocks the enrichment gate even when the name matches;
- another agency's job order is 404 on every route;
- an unconfigured provider is a structured status, never a 500.

allow-hardcode: the titles, URLs and SQL below are test fixtures.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.identity_resolutions import serper_provider
from app.main import app
from app.services import identity_resolver
from app.services.identity_resolver import Fingerprint, build_queries, resolve
from app.services.serper import (
    NormalizedSearchResult,
    SearchResultSet,
    SerperUnconfiguredError,
)
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


# --------------------------------------------------------------------------- #
# A fake provider: records queries, answers from a script.
# --------------------------------------------------------------------------- #


class FakeProvider:
    """A `WebSearchProvider` that returns canned results per query and counts
    its calls — so early stop (§32) and the cache (§34) are observable."""

    def __init__(self, answers: dict[str, list[NormalizedSearchResult]] | None = None):
        self.answers = answers or {}
        self.queries: list[str] = []

    async def search(self, query):
        self.queries.append(query.query)
        return SearchResultSet(
            query=query.query, results=self.answers.get(query.query, [])
        )


class FailingProvider:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.queries = []

    async def search(self, query):
        self.queries.append(query.query)
        raise self.exc


def result(title, url, snippet=None, query="q"):
    return NormalizedSearchResult(
        title=title,
        url=url,
        snippet=snippet,
        domain="linkedin.com",
        position=1,
        provider="serper",
        query=query,
        retrieved_at="2026-09-03T00:00:00+00:00",
    )


def fp(**overrides) -> Fingerprint:
    base = dict(
        name="Claire Sze Wei Chew",
        location="Singapore",
        current_company="Standard Chartered",
        current_title="Product Control",
        previous_companies=("Credit Suisse",),
    )
    base.update(overrides)
    return Fingerprint(**base)


# --------------------------------------------------------------------------- #
# Scoring (§15, §16)
# --------------------------------------------------------------------------- #


async def test_exact_name_employer_location_resolves():
    """§53 — the worked example: employer + location + profile is RESOLVED."""
    fp_ = fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            queries[0]: [
                result(
                    "Claire Sze Wei Chew - Product Control - Standard Chartered",
                    "https://www.linkedin.com/in/claire-chew/?trk=x",
                    "Standard Chartered, Singapore. Previously Credit Suisse.",
                )
            ]
        }
    )
    res = await resolve(fp_, provider)
    assert res.status == "resolved"
    assert res.confidence >= 85
    assert res.canonical_profile_url == "https://www.linkedin.com/in/claire-chew"
    assert res.enrichment_allowed is True
    assert res.freshness_status == "current"
    # Early stop (§32): one strong query, not the whole budget.
    assert res.queries_used == 1


async def test_name_only_never_resolves():
    """§4/§52 — a name match alone cannot reach the RESOLVED floor."""
    fp_ = fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {q: [result("Claire Chew", "https://example.com/a", "some unrelated page")] for q in queries}
    )
    res = await resolve(fp_, provider)
    assert res.status == "unresolved"
    assert res.confidence < identity_resolver.PROBABLE_MIN
    assert res.enrichment_allowed is False


async def test_contradiction_blocks_the_gate():
    """§17 — a page that fits the name but places them elsewhere blocks
    enrichment even though the name matched."""
    fp_ = fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [result("Claire Sze Wei Chew", "https://example.com/b", "London based consultant")]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert res.contradictions > 0
    assert res.enrichment_allowed is False
    assert res.status != "resolved"


async def test_probable_band_without_a_profile():
    """Name + employer + title + location with no canonical profile sits in
    PROBABLE (75), below the RESOLVED floor, and the gate still refuses
    enrichment (§61: require_resolved_identity)."""
    fp_ = fp(previous_companies=())
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Claire Sze Wei Chew Standard Chartered Product Control Singapore",
                    "https://news.example.com/x",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert identity_resolver.PROBABLE_MIN <= res.confidence < identity_resolver.RESOLVED_MIN
    assert res.status == "probable"
    assert res.enrichment_allowed is False  # REQUIRE_RESOLVED_IDENTITY (§61)


async def test_early_stop_avoids_extra_queries():
    """§9/§32 — enough confidence and no contradiction stops after one query."""
    fp_ = fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            queries[0]: [
                result(
                    "Claire Sze Wei Chew Standard Chartered Credit Suisse Singapore Profile",
                    "https://www.linkedin.com/in/claire-chew",
                )
            ]
        }
    )
    res = await resolve(fp_, provider)
    assert res.status == "resolved"
    assert len(provider.queries) == 1


async def test_query_budget_caps_the_run():
    """§31 — `normal` spends at most 3 queries even when nothing resolves."""
    fp_ = fp()
    provider = FakeProvider({})  # no answers: every query returns nothing
    res = await resolve(fp_, provider, mode="normal")
    assert res.queries_used == 3
    assert len(provider.queries) == 3
    provider2 = FakeProvider({})
    res2 = await resolve(fp_, provider2, mode="deep")
    assert res2.queries_used == 8


async def test_weights_are_configurable():
    """§15 — passing a different weight table changes the score."""
    fp_ = fp()
    queries = build_queries(fp_)
    answers = {
        queries[0]: [
            result(
                "Claire Sze Wei Chew Standard Chartered Singapore",
                "https://www.linkedin.com/in/claire-chew",
            )
        ]
    }
    base = await resolve(fp_, FakeProvider(answers))
    bumped = dict(identity_resolver.WEIGHTS)
    bumped["current_company"] = 50
    heavier = await resolve(fp_, FakeProvider(answers), weights=bumped)
    assert heavier.confidence >= base.confidence


async def test_build_queries_only_uses_present_fields():
    """§4/§8 — no name-only query is generated when a stronger field exists,
    and no query names a field the fingerprint lacks."""
    queries = build_queries(fp(current_title=None, previous_companies=()))
    assert all('"Claire Sze Wei Chew"' in q for q in queries)
    assert any("Standard Chartered" in q for q in queries)
    assert all("Credit Suisse" not in q for q in queries)


def test_fingerprint_hash_is_stable_and_field_sensitive():
    assert fp().hash() == fp().hash()
    assert fp().hash() != fp(current_company="HSBC").hash()


# --------------------------------------------------------------------------- #
# API (§34 cache, §56)
# --------------------------------------------------------------------------- #


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
            "candidate_identity_resolutions",
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


def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(client, uid, tid)
    return client


CANDIDATE = {
    "name": "Claire Sze Wei Chew",
    "current_company": "Standard Chartered",
    "current_title": "Product Control",
    "location": "Singapore",
    "previous_companies": ["Credit Suisse"],
}

_RESOLVING = {
    '"Claire Sze Wei Chew" Standard Chartered Singapore': [
        result(
            "Claire Sze Wei Chew - Product Control - Standard Chartered",
            "https://www.linkedin.com/in/claire-chew",
            "Standard Chartered Singapore. Previously Credit Suisse.",
        )
    ]
}


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider(_RESOLVING)
    app.dependency_overrides[serper_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(serper_provider, None)


async def test_resolve_persists_and_second_click_is_a_cache_hit(provider):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            path = f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity"
            res = await c.post(path, json={"mode": "normal", "candidate": CANDIDATE})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "resolved"
            assert body["confidence"] >= 85
            assert body["canonical_profile_url"].endswith("/claire-chew")
            assert body["cached"] is False
            calls_after_first = len(provider.queries)
            assert calls_after_first >= 1

            # Second click, same fingerprint → cached, no provider call (§34).
            res2 = await c.post(path, json={"mode": "normal", "candidate": CANDIDATE})
            assert res2.status_code == 200
            body2 = res2.json()
            assert body2["cached"] is True
            assert body2["id"] == body["id"]
            assert len(provider.queries) == calls_after_first

            # A refresh bypasses the cache by design (§35).
            res3 = await c.post(path, json={"mode": "refresh", "candidate": CANDIDATE})
            assert res3.json()["cached"] is False
            assert len(provider.queries) > calls_after_first

            # History shows every run, newest first; the detail route reopens one.
            hist = await c.get(f"/api/opportunities/{oid}/identity-resolutions")
            assert hist.status_code == 200
            resolutions = hist.json()["resolutions"]
            assert len(resolutions) == 2
            assert resolutions[0]["id"] == res3.json()["id"]
            detail = await c.get(
                f"/api/opportunities/{oid}/identity-resolutions/{body['id']}"
            )
            assert detail.status_code == 200
            assert detail.json()["resolution"]["candidate_key"] == "cand-1"
            assert detail.json()["resolution"]["evidence"]
    finally:
        await _drop_agency(tid)


async def test_another_agencys_job_order_is_404_on_every_route(provider):
    tid, uid = await _seed_agency()
    other_tid, other_uid = await _seed_agency()
    oid = await _opportunity(other_tid, other_uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity",
                json={"mode": "normal", "candidate": CANDIDATE},
            )
            assert res.status_code == 404
            assert (await c.get(f"/api/opportunities/{oid}/identity-resolutions")).status_code == 404
            assert (
                await c.get(
                    f"/api/opportunities/{oid}/identity-resolutions/{uuid.uuid4()}"
                )
            ).status_code == 404
    finally:
        await _drop_agency(tid)
        await _drop_agency(other_tid)


async def test_unknown_resolution_id_is_404(provider):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.get(
                f"/api/opportunities/{oid}/identity-resolutions/{uuid.uuid4()}"
            )
            assert res.status_code == 404
    finally:
        await _drop_agency(tid)


async def test_unconfigured_provider_is_a_structured_status_not_a_500(monkeypatch):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    failing = FailingProvider(SerperUnconfiguredError("no key"))
    app.dependency_overrides[serper_provider] = lambda: failing
    try:
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity",
                json={"mode": "normal", "candidate": CANDIDATE},
            )
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "unconfigured"
    finally:
        app.dependency_overrides.pop(serper_provider, None)
        await _drop_agency(tid)


async def test_missing_name_is_422(provider):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity",
                json={"mode": "normal", "candidate": {"name": ""}},
            )
            assert res.status_code == 422
    finally:
        await _drop_agency(tid)


async def test_unknown_mode_is_422(provider):
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity",
                json={"mode": "reckless", "candidate": CANDIDATE},
            )
            assert res.status_code == 422
    finally:
        await _drop_agency(tid)
