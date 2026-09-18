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

import json
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


def result(title, url, snippet=None, query="q", domain="linkedin.com"):
    return NormalizedSearchResult(
        title=title,
        url=url,
        snippet=snippet,
        domain=domain,
        position=1,
        provider="serper",
        query=query,
        retrieved_at="2026-09-03T00:00:00+00:00",
    )


def fp(**overrides) -> Fingerprint:
    base: dict = dict(
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
        {
            q: [result("Claire Chew", "https://example.com/a", "some unrelated page")]
            for q in queries
        }
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


def test_hash_ignores_context_but_tracks_skills():
    """The cache key must survive a reworded summary (context out) but change
    when the skills change (they anchor queries)."""
    assert fp(context="a summary").hash() == fp(context="a different summary").hash()
    assert fp(skills=("Python",)).hash() != fp(skills=("Go",)).hash()


# --------------------------------------------------------------------------- #
# Full-context enrichment (§4, §6)
# --------------------------------------------------------------------------- #


def test_skill_anchored_query_when_no_company():
    """§4/§8 — with no employer, a named skill plus location anchors the query,
    never the bare name."""
    queries = build_queries(
        fp(current_company=None, current_title=None, previous_companies=(),
           skills=("Product Control", "Basel III"))
    )
    assert queries, "a title/skill/location fingerprint must still produce queries"
    assert all('"Claire Sze Wei Chew"' in q for q in queries)
    assert any("Product Control" in q for q in queries)
    assert '"Claire Sze Wei Chew"' not in queries  # never the bare name


def test_source_profile_query_is_emitted():
    queries = build_queries(
        fp(source_profile_url="https://www.linkedin.com/in/claire-chew",
           source_provider="linkedin.com")
    )
    assert "https://www.linkedin.com/in/claire-chew" in queries
    assert any("site:linkedin.com" in q for q in queries)


def test_name_only_fingerprint_builds_no_queries():
    """§4 — no company/title/location/skill/profile = nothing to search."""
    empty = fp(
        current_company=None, current_title=None, location=None,
        previous_companies=(), skills=(),
    )
    assert build_queries(empty) == []
    assert empty.has_context() is False


def test_context_terms_are_distinctive_and_bounded():
    """§6 — context mining drops covered attributes, stopwords and short words,
    and never returns more than three terms."""
    fp_ = fp(
        context=(
            "exact domain match with deep tenure across years of "
            "product control in Singapore"
        )
    )
    terms = identity_resolver.context_terms(fp_)
    assert terms == ["domain", "tenure"]
    assert all(len(t) >= 4 for t in terms)


def test_context_anchored_query_when_company_missing():
    """§6/§4 — with no employer, a distinctive prose term plus location still
    produces a short, name-anchored query (never the bare name)."""
    fp_ = fp(
        current_company=None, current_title=None, previous_companies=(),
        skills=(), location="Singapore",
        context="exact domain match with deep tenure in product control",
    )
    queries = build_queries(fp_)
    anchor = identity_resolver.context_terms(fp_)[0]
    assert any(f'"{anchor}"' in q and "Singapore" in q for q in queries)
    assert '"Claire Sze Wei Chew"' not in queries


def test_context_terms_score_and_break_ties():
    """§15 — a page carrying the employer *and* distinctive prose terms
    outscores an otherwise identical page carrying the employer alone."""
    fp_ = andrew_fp(
        context=(
            "14+ years Product Control at UBS and Barclays in Singapore; "
            "exact domain match with deep tenure"
        )
    )
    rich = result(
        "Andrew Ng - Product Control - UBS",
        "https://www.vault.example/andrew-ng",
        "Andrew Ng, UBS and Barclays, Singapore. Deep tenure in this domain.",
    )
    bare = result(
        "Andrew Ng - Product Control - UBS",
        "https://www.plain.example/andrew-ng",
        "Andrew Ng, UBS, Singapore.",
    )
    W = identity_resolver.WEIGHTS
    rich_ev = identity_resolver.extract_evidence(rich, fp_, W)
    bare_ev = identity_resolver.extract_evidence(bare, fp_, W)
    assert sum(e.weight for e in rich_ev) > sum(e.weight for e in bare_ev)
    assert "context" in {e.type for e in rich_ev}



async def test_name_only_resolve_makes_no_provider_call():
    """§4 — a name-only fingerprint never reaches Serper and is unresolved."""
    empty = fp(
        current_company=None, current_title=None, location=None,
        previous_companies=(), skills=(),
    )
    provider = FakeProvider({})
    res = await resolve(empty, provider)
    assert res.status == "needs_context"
    assert res.queries_used == 0
    assert provider.queries == []
    assert res.confidence == 0


async def test_skill_match_contributes_bounded_evidence():
    """§15 — a matched skill adds evidence, capped so it cannot alone resolve."""
    fp_ = fp(current_company=None, current_title=None, previous_companies=(),
             location="Singapore", skills=("Product Control", "Basel III"))
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Claire Sze Wei Chew Product Control Basel III Singapore",
                    "https://news.example.com/x",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    skill_types = [e.type for e in res.evidence]
    assert "skills" in skill_types
    skill_ev = next(e for e in res.evidence if e.type == "skills")
    assert skill_ev.weight <= identity_resolver.WEIGHTS["skills"]


async def test_source_profile_url_match_is_strong_evidence():
    """§6 — a result whose URL is the discovered source profile scores high."""
    url = "https://www.linkedin.com/in/claire-chew"
    fp_ = fp(source_profile_url=url)
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [result("Claire Sze Wei Chew", url)]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert res.canonical_profile_url == url
    assert any(e.type == "source_profile" for e in res.evidence)


# --------------------------------------------------------------------------- #
# The famous-stranger case (2026-09)
# --------------------------------------------------------------------------- #


def andrew_fp(**overrides) -> Fingerprint:
    base: dict = dict(
        name="Andrew Ng",
        location="Singapore",
        current_company="UBS",
        current_title="Product Control",
        previous_companies=("Barclays",),
    )
    base.update(overrides)
    return Fingerprint(**base)


async def test_famous_stranger_does_not_resolve():
    """A same-name LinkedIn page with no employer/title/location corroboration
    must not resolve. Regression: 'Andrew Ng' at UBS resolved to
    linkedin.com/in/andrewyng (the AI researcher, USA) at 55%."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng - Stanford University - AI researcher",
                    "https://www.linkedin.com/in/andrewyng",
                    "Andrew Ng is a professor at Stanford University, founder of Coursera.",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert res.status == "unresolved"
    assert res.confidence < identity_resolver.PROBABLE_MIN
    assert res.canonical_profile_url is None
    assert res.freshness_status == "unknown"
    assert res.enrichment_allowed is False
    # The same-name profile is recorded, visible, and scores nothing.
    same = [e for e in res.evidence if e.type == "same_name_profile"]
    assert same and all(e.weight == 0 for e in same)
    assert not any(e.type == "profile_url" for e in res.evidence)


async def test_same_name_profile_with_employer_corroboration_counts():
    """The control: same shape, but the page names the candidate's employer —
    the profile signal then counts and confidence rises."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng - Product Control - UBS Singapore",
                    "https://www.linkedin.com/in/andrew-ng-ubs",
                    "Product Control at UBS in Singapore. Previously Barclays.",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert res.canonical_profile_url == "https://www.linkedin.com/in/andrew-ng-ubs"
    assert any(e.type == "profile_url" for e in res.evidence)
    assert not any(e.type == "same_name_profile" for e in res.evidence)
    assert res.confidence >= identity_resolver.RESOLVED_MIN
    assert res.freshness_status == "current"


async def test_freshness_is_unknown_without_company_and_no_contradiction():
    """§21 — a name match plus a profile/nothing never reads as
    `possible_change`; with no employer evidence and no contradiction it is
    `unknown`."""
    fp_ = andrew_fp(current_company=None, previous_companies=())
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng Singapore",
                    "https://www.linkedin.com/in/andrewyng",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert res.freshness_status == "unknown"
    assert res.contradictions == 0


async def test_freshness_possible_change_only_on_contradiction():
    """§21 — `possible_change` requires active contradicting evidence."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng",
                    "https://example.com/profile",
                    "London based consultant.",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert res.contradictions > 0
    assert res.freshness_status == "possible_change"


# --------------------------------------------------------------------------- #
# Aggregator over-merge, Tier-C trust and ambiguity (2026-09-03)
#
# Scoring math after these fixes, for `andrew_fp()` (UBS / Product Control /
# Singapore / prev Barclays) on a RocketReach page naming all of it:
#   name 15→7, company 25→12, title 20→10, location 15→7, previous 10→5,
#   career_history 10→5  ⇒ 46, well under PROBABLE_MIN(70).
# Tier C halves and rounds down every positive weight; contradictions keep full
# magnitude. Three mirrored RocketReach pages therefore score 46 each, not one
# 100-point stack.
# --------------------------------------------------------------------------- #


def _rocketreach(slug: str) -> NormalizedSearchResult:
    return result(
        "Andrew Ng - Product Control - UBS",
        f"https://rocketreach.co/andrew-ng-{slug}",
        "Andrew Ng, UBS and Barclays, Singapore.",
        domain="rocketreach.co",
    )


async def test_aggregator_pages_do_not_merge_into_one_cluster():
    """§18 — three person-scoped RocketReach profile URLs are three people (or
    at least three claims), never one cluster. Folding them by host previously
    stacked name+company per URL into a false RESOLVED."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    pages = [_rocketreach("a"), _rocketreach("b"), _rocketreach("c")]
    provider = FakeProvider({q: list(pages) for q in queries})
    res = await resolve(fp_, provider)

    agg_clusters = [c for c in res.clusters if c.key.startswith("agg:")]
    assert len(agg_clusters) == 3, "each aggregator URL is its own cluster"
    # Tier-C halving keeps each page modest — none reaches PROBABLE alone.
    assert all(c.score < identity_resolver.PROBABLE_MIN for c in agg_clusters)
    assert res.confidence < identity_resolver.RESOLVED_MIN
    assert res.status != "resolved"
    assert res.enrichment_allowed is False


async def test_tier_c_halves_positive_signals():
    """§23 — the same page scores roughly half on an aggregator domain as it
    does on a first-party domain."""
    fp_ = andrew_fp()
    W = identity_resolver.WEIGHTS
    tier_b = result(
        "Andrew Ng - Product Control - UBS",
        "https://www.ubs.example/andrew-ng",
        "Andrew Ng, UBS and Barclays, Singapore.",
        domain="ubs.example",
    )
    tier_c = result(
        "Andrew Ng - Product Control - UBS",
        "https://rocketreach.co/andrew-ng",
        "Andrew Ng, UBS and Barclays, Singapore.",
        domain="rocketreach.co",
    )
    b = sum(e.weight for e in identity_resolver.extract_evidence(tier_b, fp_, W))
    c = sum(e.weight for e in identity_resolver.extract_evidence(tier_c, fp_, W))
    assert c < b
    assert c <= b // 2


async def test_two_distinct_profiles_downgrade_to_probable():
    """§17 — two distinct person-scoped identities both at PROBABLE or above
    means we cannot say *which* person this is. The score stays (it is the best
    candidate's) but the status must be `probable` and no canonical profile is
    offered — a 93% RESOLVED on an ambiguous name must not stand."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            queries[0]: [
                result(
                    "Andrew Ng - Product Control - UBS Singapore",
                    "https://www.linkedin.com/in/andrew-ng-ubs",
                    "Product Control at UBS in Singapore. Previously Barclays.",
                ),
                result(
                    "Andrew Ng - Product Control - UBS Singapore",
                    "https://www.linkedin.com/in/andrew-ng-barclays",
                    "Product Control at UBS in Singapore. Previously Barclays.",
                ),
            ]
        }
    )
    res = await resolve(fp_, provider)
    assert res.confidence >= identity_resolver.PROBABLE_MIN
    assert res.status == "probable"
    assert res.canonical_profile_url is None
    assert res.freshness_status == "unknown"
    assert res.enrichment_allowed is False


async def test_other_location_suppresses_location_credit():
    """§17 query-echo: a snippet quoting the query's "Singapore" *and* a real
    "White Plains, New York" must not earn the +15 location signal."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng",
                    "https://rocketreach.co/andrew-ng-ny",
                    "Andrew Ng, Singapore. Global Markets Business Manager, "
                    "White Plains, New York, USA.",
                    domain="rocketreach.co",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert not any(e.type == "location" for e in res.evidence)


async def test_new_york_location_contradicts_singapore_fingerprint():
    """§17 — a page placing the name only in New York contradicts a Singapore
    fingerprint: no location credit, and the contradiction fires."""
    fp_ = andrew_fp()
    queries = build_queries(fp_)
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng",
                    "https://example.com/andrew-ng",
                    "Andrew Ng, Global Markets Business Manager, "
                    "White Plains, New York, USA.",
                )
            ]
            for q in queries
        }
    )
    res = await resolve(fp_, provider)
    assert not any(e.type == "location" for e in res.evidence)
    assert res.contradictions > 0
    assert res.enrichment_allowed is False


async def test_andrew_ng_live_shape_is_at_most_probable_with_no_profile():
    """Acceptance gate (1): the observed live shape — RocketReach-heavy, the
    real UBS/Barcalys profile in White Plains NY, plus other Singapore namesakes
    — must not resolve and must offer no canonical profile."""
    fp_ = andrew_fp()
    provider = FakeProvider(
        {
            q: [
                result(
                    "Andrew Ng - Product Control - UBS",
                    "https://rocketreach.co/andrew-ng-1",
                    "Andrew Ng, UBS, Product Control, Singapore.",
                    domain="rocketreach.co",
                ),
                result(
                    "Andrew Ng - Barclays",
                    "https://rocketreach.co/andrew-ng-2",
                    "Andrew Ng, Product Control, Singapore.",
                    domain="rocketreach.co",
                ),
                result(
                    "Andrew Ng - Global Markets Business Manager, US/London "
                    "Credit Product Controller",
                    "https://www.linkedin.com/in/andrew-ng-ny",
                    "Andrew Ng, UBS 14 yrs 9 mos, Global Markets Business "
                    "Manager. White Plains, New York, USA.",
                ),
                result(
                    "Andrew Ng - DBS Group Executive",
                    "https://www.linkedin.com/in/andrew-ng-sg",
                    "Andrew Ng, DBS Group Executive, Operational Risk AVP, "
                    "Singapore.",
                ),
            ]
            for q in build_queries(fp_)
        }
    )
    res = await resolve(fp_, provider)
    assert res.status in ("probable", "unresolved")
    assert res.status != "resolved"
    assert res.canonical_profile_url is None
    assert res.enrichment_allowed is False


async def test_title_fragment_never_scores_or_displays():
    """§15 — a single-word title fragment must not score.

    Regression: the fingerprint's "Product Control" credited +20 to any page
    that merely said "product". The match now requires the full phrase (both
    "product" and "control"), so a page naming only "product" yields no title
    evidence at all — and nothing for the modal to display.
    """
    fp_ = andrew_fp(previous_companies=(), location=None)
    partial = result(
        "Andrew Ng - product analyst",
        "https://www.linkedin.com/in/andrew-ng-product",
        "Andrew Ng works on product.",
    )
    ev = identity_resolver.extract_evidence(partial, fp_, identity_resolver.WEIGHTS)
    assert not any(e.type == "title" for e in ev)

    full = result(
        "Andrew Ng - Product Control",
        "https://www.linkedin.com/in/andrew-ng-pc",
        "Andrew Ng, Product Control.",
    )
    ev_full = identity_resolver.extract_evidence(full, fp_, identity_resolver.WEIGHTS)
    assert any(e.type == "title" for e in ev_full)


async def test_stable_ambiguity_stops_early_in_refresh_mode():
    """§32 — a stably ambiguous run must not spend the whole refresh budget.

    Two distinct LinkedIn profiles both clear PROBABLE but neither resolves and
    the leader never changes, so the run stops at 3 queries (not the refresh
    budget of 5) and records the queries actually used.
    """
    fp_ = andrew_fp(previous_companies=())
    queries = build_queries(fp_)
    assert len(queries) >= 5, "refresh budget must offer more than 3 queries"
    page = [
        result(
            "Andrew Ng - Product Control - UBS",
            "https://www.linkedin.com/in/andrew-ng-a",
            "Andrew Ng, Product Control at UBS.",
        ),
        result(
            "Andrew Ng - Product Control - UBS",
            "https://www.linkedin.com/in/andrew-ng-b",
            "Andrew Ng, Product Control at UBS.",
        ),
    ]
    provider = FakeProvider({q: list(page) for q in queries})
    res = await resolve(fp_, provider, mode="refresh")
    assert res.status != "resolved"
    assert res.queries_used == 3
    assert len(provider.queries) == 3


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
            assert (
                await c.get(f"/api/opportunities/{oid}/identity-resolutions")
            ).status_code == 404
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


async def test_name_only_candidate_is_needs_context_with_no_provider_call(provider):
    """§4 — the route answers needs_context for a name-only fingerprint, makes
    no Serper call, and persists nothing."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    before = len(provider.queries)
    try:
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity",
                json={"mode": "normal", "candidate": {"name": "Claire Sze Wei Chew"}},
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "needs_context"
            assert "name alone" in body["message"]
            assert len(provider.queries) == before
            # Nothing persisted — the history stays empty.
            hist = await c.get(f"/api/opportunities/{oid}/identity-resolutions")
            assert hist.json()["resolutions"] == []
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


# --------------------------------------------------------------------------- #
# Reveal Contact (§26, Phase 3 first slice)
# --------------------------------------------------------------------------- #

# A page that names the candidate and publicly lists an address, so both the
# resolve (identity) and reveal (contact) halves have something to show.
_RESOLVING_WITH_EMAIL = {
    '"Claire Sze Wei Chew" Standard Chartered Singapore': [
        result(
            "Claire Sze Wei Chew - Product Control - Standard Chartered",
            "https://www.linkedin.com/in/claire-chew",
            "Standard Chartered Singapore. Previously Credit Suisse. "
            "Reach Claire at claire.chew@example-bank.com.",
        )
    ]
}


async def _insert_resolution(
    tenant_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    status: str,
    evidence: list[dict],
    public_emails: list[dict] | None = None,
) -> uuid.UUID:
    """Insert one resolution row directly — the gate reads the *stored* row, so
    these tests set the row's shape rather than re-deriving it through Serper."""
    rid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_identity_resolutions (id, tenant_id,"
                " opportunity_id, candidate_key, fingerprint, fingerprint_hash,"
                " status, confidence, evidence, public_emails, queries_used,"
                " expires_at, created_by)"
                " VALUES (:i, :t, :o, 'cand-1', '{}'::jsonb, :h, :s, 90,"
                " CAST(:e AS jsonb), CAST(:p AS jsonb), 2,"
                " now() + interval '30 days', :u)"
            ),
            {
                "i": rid,
                "t": tenant_id,
                "o": opportunity_id,
                "h": str(rid).replace("-", "")[:64],
                "s": status,
                "e": json.dumps(evidence),
                "p": json.dumps(public_emails or []),
                "u": user_id,
            },
        )
        await s.commit()
    return rid


async def test_resolve_surfaces_public_emails(provider):
    """§26 — an address in a result snippet is stored unverified and returned."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    fake = FakeProvider(_RESOLVING_WITH_EMAIL)
    app.dependency_overrides[serper_provider] = lambda: fake
    try:
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/candidates/cand-1/resolve-identity",
                json={"mode": "normal", "candidate": CANDIDATE},
            )
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "resolved"
            emails = res.json()["public_emails"]
            assert emails == [
                {
                    "email": "claire.chew@example-bank.com",
                    "source_url": "https://www.linkedin.com/in/claire-chew",
                    "verified": False,
                }
            ]
    finally:
        app.dependency_overrides.pop(serper_provider, None)
        await _drop_agency(tid)


async def test_reveal_contact_noop_on_resolved(provider):
    """The shipped default: a resolved identity reveals with `no_provider` and
    the vendor-setup sentence, plus the free public emails."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        rid = await _insert_resolution(
            tid,
            oid,
            uid,
            status="resolved",
            evidence=[
                {"type": "name", "value": "Claire", "weight": 15},
                {"type": "current_company", "value": "SC", "weight": 25},
            ],
            public_emails=[
                {
                    "email": "claire@example-bank.com",
                    "source_url": "https://x.example.org",
                    "verified": False,
                }
            ],
        )
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/identity-resolutions/{rid}/reveal-contact"
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "no_provider"
            assert body["provider"] == "none"
            assert "ContactOut" in body["message"]
            assert body["emails"] == []
            assert body["public_emails"][0]["email"] == "claire@example-bank.com"
            assert body["verified"] is False

            # Persisted: reopening the resolution shows the enrichment answer.
            detail = await c.get(
                f"/api/opportunities/{oid}/identity-resolutions/{rid}"
            )
            assert detail.json()["resolution"]["contact_enrichment"]["status"] == (
                "no_provider"
            )
    finally:
        await _drop_agency(tid)


async def test_reveal_contact_refused_on_probable_with_no_provider_call(provider):
    """§17 — a `probable` identity is a 409 with a reason, and no provider runs.

    The Noop makes no network call, so "no call" is asserted by the absence of a
    stored `contact_enrichment`: a refused reveal must leave the row untouched.
    """
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        rid = await _insert_resolution(
            tid,
            oid,
            uid,
            status="probable",
            evidence=[{"type": "name", "value": "Claire", "weight": 15}],
        )
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/identity-resolutions/{rid}/reveal-contact"
            )
            assert res.status_code == 409, res.text
            assert "resolved" in res.json()["detail"]
            detail = await c.get(
                f"/api/opportunities/{oid}/identity-resolutions/{rid}"
            )
            assert detail.json()["resolution"]["contact_enrichment"] is None
    finally:
        await _drop_agency(tid)


async def test_reveal_contact_refused_on_contradiction(provider):
    """A stored contradiction blocks enrichment even at `resolved` (§17)."""
    tid, uid = await _seed_agency()
    oid = await _opportunity(tid, uid)
    try:
        rid = await _insert_resolution(
            tid,
            oid,
            uid,
            status="resolved",
            evidence=[
                {"type": "name", "value": "Claire", "weight": 15},
                {
                    "type": "contradiction",
                    "value": "Other Co",
                    "weight": -30,
                },
            ],
        )
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/identity-resolutions/{rid}/reveal-contact"
            )
            assert res.status_code == 409, res.text
            assert "conflict" in res.json()["detail"].lower()
    finally:
        await _drop_agency(tid)


async def test_reveal_contact_404_for_another_agencys_resolution(provider):
    tid, uid = await _seed_agency()
    other_tid, other_uid = await _seed_agency()
    oid = await _opportunity(other_tid, other_uid)
    try:
        rid = await _insert_resolution(
            other_tid, oid, other_uid, status="resolved", evidence=[]
        )
        async with _http(tid, uid) as c:
            res = await c.post(
                f"/api/opportunities/{oid}/identity-resolutions/{rid}/reveal-contact"
            )
            assert res.status_code == 404
    finally:
        await _drop_agency(tid)
        await _drop_agency(other_tid)
