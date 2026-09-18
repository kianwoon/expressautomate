"""Candidate Identity Resolver — deterministic identity resolution over Serper.

Spec: `serper design.md` §6 (fingerprint), §7–9 (progressive sequential
search + early stop), §13–17 (evidence, weighting, confidence thresholds,
hard safety), §18–20 (entity clustering, URL canonicalization, LinkedIn),
§21 (freshness), §31 (budget), §50–52 (normalisation).

The one rule that shapes every decision here: **the LLM is not in this loop**
(§49). Confidence is a deterministic sum of weighted evidence, so the same
fingerprint and the same search results always yield the same number. An LLM
may later assist with extraction, but it may never "declare" an identity.

Two more rules are structural, not advisory:

- **Name-only never resolves.** The exact-name signal is worth 15 (§15) and
  the PROBABLE floor is 70, so a candidate whose only evidence is a name match
  cannot leave UNRESOLVED — the maths enforces §52 rather than a comment.
- **A contradiction blocks the gate.** A result that fits the name but names a
  different employer/location is a different person; it trips
  `contradiction` and the safety gate (§17) refuses enrichment regardless of
  the top cluster's score.

Weights live in `WEIGHTS` (§15) and are passed in as data, never read from
module constants inside the scoring function — §15 requires them configurable.
"""

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from app.services.contact_extract import extract_public_emails as _extract_public_emails
from app.services.serper import (
    NormalizedSearchResult,
    SearchQuery,
    WebSearchProvider,
)

# --- §15 evidence weights (configurable; the resolver reads them from here) ---

WEIGHTS: dict[str, int] = {
    "name": 15,
    "current_company": 25,
    "title": 20,
    "location": 15,
    "previous_company": 10,
    "career_history": 10,
    "profile_url": 15,
    "education": 5,
    # §15 skill signal: a page corroborating a named skill is real identity
    # evidence, but capped low — a common skill ("AWS") appears on thousands of
    # unrelated pages, so this can nudge a cluster, never resolve one alone.
    "skills": 10,
    # §6/§15 context signal: a distinctive word mined from the free-prose
    # context ("tenure", "domain") that appears on a result page is mild
    # corroboration. +3 per matched term, at most 3 terms (+9) — enough to break
    # a tie between two same-name pages, never enough to resolve one alone.
    "context": 3,
    "employer_contradiction": -30,
    "location_contradiction": -25,
    "role_contradiction": -20,
}

# --- §23 source trust tiers (unimplemented until now) -----------------------
#
# Not every page is equal evidence. The tier decides the multiplier applied to
# every *positive* signal extracted from a result; contradictions keep full
# magnitude (a wrong-person page is a red flag whoever hosts it).
#
#   Tier A — full weight: the employer's own site, professional associations,
#            universities, conference programmes. First-party or curated.
#   Tier B — full weight: LinkedIn / GitHub / personal portfolios. A person's
#            own hand-maintained page.
#   Tier C — half weight: people-aggregators and scraped directories
#            (RocketReach, ZoomInfo, SignalHire, Apollo, Lusha …). They mirror
#            each other, so three of them are not three independent sightings.
#
# The multiplier is a module constant so it is configurable in one place, next
# to `WEIGHTS`, not recomputed per call.
TIER_C_MULTIPLIER = 0.5

# People-aggregator profile domains. Their *profile* URLs are person-scoped the
# way a LinkedIn `/in/` URL is, so they must cluster per URL — see
# `cluster_key`. This is a subset of the Tier C trust set.
AGGREGATOR_DOMAINS = frozenset(
    {
        "rocketreach.co",
        "zoominfo.com",
        "signalhire.com",
        "apollo.io",
        "lusha.com",
        "uplead.com",
        "leadiq.com",
        "contactout.com",
        "hunter.io",
        "clearbit.com",
        "peopledatalabs.com",
        "wiza.co",
        "kaspr.io",
        "seamless.ai",
        "spokeo.com",
    }
)

# Tier C trust domains: the aggregators above plus the scraped-directory
# family. Matched on the registrable suffix, so `www.rocketreach.co` and
# `rocketreach.co` are both Tier C.
TIER_C_DOMAINS = AGGREGATOR_DOMAINS | frozenset(
    {"crunchbase.com", "companieshouse.gov.uk", "opencorporates.com", "dnb.com"}
)


def _matches_domain(domain: str | None, domains: frozenset[str]) -> bool:
    """Whether `domain` is one of `domains` or a subdomain of one."""
    host = (domain or "").lower().strip().lstrip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)

# --- §16 confidence thresholds and §31 query budget ---

RESOLVED_MIN = 85
PROBABLE_MIN = 70

QUERY_BUDGET: dict[str, int] = {"normal": 3, "refresh": 5, "deep": 8}

# §32 early stop — stop once we are at RESOLVED and nothing contradicts.
EARLY_STOP_MIN = 85

# §61 enrichment gate default: contact lookup needs a RESOLVED identity.
REQUIRE_RESOLVED_IDENTITY = True

# Location names we treat as the Singapore market (§52/§61). A candidate whose
# location string mentions one of these matches the location signal; a result
# that places the name somewhere else entirely is a location contradiction.
_SINGAPORE = {"singapore", "sg", "s'pore"}

# High-precision list of other major markets (§17). Deliberately not a global
# gazetteer — an explicit, well-known market is the signal we trust. Tokens are
# normalized whole words, so multi-word places are joined ("newyork",
# "whiteplains", "unitedstates").
_OTHER_LOCATIONS = frozenset(
    {
        # North America
        "newyork", "newyorkcity", "nyc", "unitedstates", "usa",
        "america", "whiteplains", "california", "sanfrancisco", "chicago",
        "boston", "seattle", "houston", "toronto", "canada",
        # Europe
        "london", "paris", "berlin", "frankfurt", "zurich", "geneva",
        "amsterdam", "madrid", "milan", "luxembourg", "dublin", "edinburgh",
        "manchester", "unitedkingdom", "uk", "england", "germany",
        "switzerland", "france",
        # Asia / Middle East / Oceania
        "hongkong", "tokyo", "osaka", "shanghai", "beijing", "shenzhen",
        "mumbai", "bangalore", "bengaluru", "delhi", "chennai", "kolkata",
        "dubai", "abudhabi", "doha", "riyadh", "seoul", "taipei", "jakarta",
        "bangkok", "manila", "kualalumpur", "sydney", "melbourne", "auckland",
    }
)

# Institutions that count as the §15 education signal.
_EDU_HINTS = (
    "university",
    "univ",
    "nus",
    "ntu",
    "smu",
    "polytechnic",
    "college",
    "mba",
    "b.sc",
    "bsc",
    "master",
    "degree",
)

# Words that carry no identity signal when comparing titles/employers.
_STOPWORDS = {
    "the", "of", "and", "at", "in", "for", "group", "ag", "inc", "ltd",
    "pte", "limited", "holdings", "bank", "banking", "company", "co",
}

_WORD = re.compile(r"[a-z0-9]+")

# §6 context mining: prose filler that carries no identity signal. These are
# dropped before a context term is chosen so a distinctive word like "tenure"
# or "domain" is not crowded out by "years" or "exact" from a summary sentence.
_CONTEXT_STOPWORDS = _STOPWORDS | {
    "years", "year", "exact", "deep", "match", "matches", "matching", "strong",
    "very", "more", "most", "well", "also", "plus", "high", "senior", "based",
    "work", "works", "worked", "working", "role", "roles", "team", "teams",
    "across", "within", "around", "including", "over", "than", "with", "from",
    "this", "that", "their", "they", "have", "has", "been", "will", "into",
    "track", "record", "proven", "hands", "hand", "level", "large", "scale",
}


def context_terms(fp: "Fingerprint", limit: int = 3) -> list[str]:
    """§6 — distinctive single-word terms mined from the free-prose `context`.

    Only words the fingerprint has not already captured in the name, employer,
    title, location or previous employers qualify, so a term adds genuinely new
    signal rather than restating an attribute that already anchors a query.
    Deterministic and cheap: no LLM, first-seen order, capped at `limit`.
    """
    if not fp.context:
        return []
    covered = (
        name_tokens(fp.name)
        | org_tokens(fp.current_company)
        | name_tokens(fp.current_title)
        | name_tokens(fp.location)
        | {t for p in fp.previous_companies for t in org_tokens(p)}
        | {t for s in fp.skills for t in name_tokens(s)}
    )
    out: list[str] = []
    for token in normalize_text(fp.context).split():
        if len(token) < 4 or token in _CONTEXT_STOPWORDS or token in covered:
            continue
        if token not in out:
            out.append(token)
        if len(out) >= limit:
            break
    return out



# --------------------------------------------------------------------------- #
# Normalisation (§50, §51)
# --------------------------------------------------------------------------- #


def normalize_text(value: str | None) -> str:
    """Lowercase, strip accents, collapse punctuation to spaces."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(_WORD.findall(ascii_only.lower()))


def name_tokens(value: str | None) -> set[str]:
    return {t for t in normalize_text(value).split() if t}


def org_tokens(value: str | None) -> set[str]:
    """Company tokens with corporate suffixes and stopwords dropped, so
    `UBS AG`, `UBS` and `UBS Group` compare equal (§50)."""
    return {t for t in normalize_text(value).split() if t not in _STOPWORDS}


# Minimum token length that can carry identity signal. A two-character fragment
# ("ag", "sg", "co") is a suffix or an artefact, never the distinctive part of
# an employer or role.
_SIGNAL_MIN_LEN = 3

# A title only counts when its FULL phrase is present — at least two surviving
# tokens, all of which the page must name. A single leftover token ("product"
# from "Product Control") is a fragment, not a title: crediting it would let an
# unrelated page that merely says "product" earn the +20 title signal.
_TITLE_MIN_TOKENS = 2


def signal_tokens(value: str | None) -> set[str]:
    """Non-stopword tokens of `value` long enough to carry identity signal."""
    return {t for t in (name_tokens(value) - _STOPWORDS) if len(t) >= _SIGNAL_MIN_LEN}


def title_phrase_tokens(value: str | None) -> set[str]:
    """Full-phrase title tokens, or empty when the title cannot qualify.

    Requires the whole phrase — every surviving token must appear on the page
    (`_contains`) — and at least `_TITLE_MIN_TOKENS` of them. "Product Control"
    therefore scores only when a page names both "product" and "control"; a
    page naming just "product" scores nothing and the modal shows nothing.
    """
    toks = signal_tokens(value)
    return toks if len(toks) >= _TITLE_MIN_TOKENS else set()


def company_phrase_tokens(value: str | None) -> set[str]:
    """Company tokens: min one meaningful token, short fragments rejected.

    `UBS`, `HSBC` and `OCBC` survive; a two-character fragment does not, so a
    stray "ag" from `UBS AG` can never anchor a company match on its own.
    """
    return signal_tokens(value)


def canonical_url(url: str) -> str:
    """§19 — http→https, drop query/fragment/trailing slash, lowercase host."""
    if not url:
        return ""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    return f"{scheme}://{host}{path}" if host else url.strip()


def is_professional_profile(url: str) -> bool:
    """§20 — an indexed LinkedIn `/in/` profile is the canonical identity."""
    return "linkedin.com/in/" in canonical_url(url)


def cluster_key(url: str) -> str:
    """§18 — the key two results share when they describe the same person.

    A LinkedIn profile URL is person-scoped, so it clusters on its own. A
    *people-aggregator* profile URL (RocketReach, ZoomInfo …) is person-scoped
    in exactly the same way: `/p/andrew-ng` and `/p/andrew-ng-2` are two people.
    Folding them by host merged three distinct profiles into one cluster and
    stacked their name+company signals into a false RESOLVED, so aggregators
    also cluster per canonical URL.

    Every other URL clusters by host: a company bio and a conference page on
    the same domain are one identity, two pages."""
    canon = canonical_url(url)
    if is_professional_profile(canon):
        return f"li:{canon}"
    from urllib.parse import urlsplit

    host = (urlsplit(canon).hostname or "") if canon else ""
    if _matches_domain(host, AGGREGATOR_DOMAINS):
        return f"agg:{canon}"
    return f"dom:{host}"


# --------------------------------------------------------------------------- #
# Fingerprint (§6)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fingerprint:
    name: str
    current_company: str | None = None
    current_title: str | None = None
    location: str | None = None
    previous_companies: tuple[str, ...] = ()
    # §6 enrichment (2026-09): the resolver uses the candidate's FULL data, not
    # a name alone. `skills` anchors queries when no employer is known;
    # `context` (summary/match_reason prose) stays out of the hash so a reworded
    # summary does not miss the §34 cache; a source profile URL is a discovered
    # identity we can search around.
    skills: tuple[str, ...] = ()
    context: str | None = None
    source_profile_url: str | None = None
    source_provider: str | None = None

    @classmethod
    def from_body(cls, candidate: dict) -> "Fingerprint":
        prev = candidate.get("previous_companies") or []
        skills = candidate.get("skills") or []
        return cls(
            name=str(candidate.get("name") or "").strip(),
            current_company=_clean(candidate.get("current_company")),
            current_title=_clean(candidate.get("current_title")),
            location=_clean(candidate.get("location")),
            previous_companies=tuple(
                c for c in (_clean(p) for p in prev) if c
            ),
            skills=tuple(s for s in (_clean(s) for s in skills) if s),
            context=_clean(candidate.get("context")),
            source_profile_url=_clean(candidate.get("source_profile_url")),
            source_provider=_clean(candidate.get("source_provider")),
        )

    def has_context(self) -> bool:
        """Whether anything beyond the bare name is known.

        §4: a name-only fingerprint must not be searched — Serper would return
        a page of strangers and the whole run is wasted budget. Employer,
        title, location, a skill, or a discovered profile URL all count.
        """
        return bool(
            self.current_company
            or self.current_title
            or self.location
            or self.previous_companies
            or self.skills
            or self.source_profile_url
        )

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "current_company": self.current_company,
            "current_title": self.current_title,
            "location": self.location,
            "previous_companies": list(self.previous_companies),
            "skills": list(self.skills),
            "context": self.context,
            "source_profile_url": self.source_profile_url,
            "source_provider": self.source_provider,
        }

    def hash(self) -> str:
        """§34 cache key: normalized name + company + title + location + skills.

        `context` is deliberately excluded: it is free prose that can be
        reworded without changing who the candidate is, and hashing it would
        miss a cache hit on every edit.
        """
        material = "|".join(
            (
                normalize_text(self.name),
                normalize_text(self.current_company),
                normalize_text(self.current_title),
                normalize_text(self.location),
                normalize_text(" ".join(self.skills)),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _clean(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# --------------------------------------------------------------------------- #
# Adaptive query generation (§7, §8)
# --------------------------------------------------------------------------- #


def _provider_domain(profile_url: str, provider: str | None) -> str | None:
    """The domain to `site:`-scope a source-profile discovery query to.

    `source_provider` is a display label ("LinkedIn") more often than a host,
    so the profile URL's own host is the reliable source; the label is only
    used when it already looks like a domain. Returns None when neither gives
    a usable host, so no malformed `site:` query is ever emitted."""
    host = canonical_url(profile_url).split("://")[-1].split("/")[0]
    if host:
        return host
    if provider and "." in provider and " " not in provider:
        return provider
    return None


def build_queries(fp: Fingerprint) -> list[str]:
    """Ordered, progressively more specific queries (§7–§8).

    Only queries whose fields exist are built, and a query always pairs the
    name with **at least one other attribute** — a bare `"name"` query returns
    a page of strangers and is exactly what §4 forbids. When the fingerprint is
    name-only this returns `[]`: the route then answers a structured
    `needs_context` status instead of spending a Serper call. Queries stay
    short (§8): at most name + two attributes.
    """
    name = fp.name
    company = fp.current_company
    title = fp.current_title
    location = fp.location
    skills = list(fp.skills[:3])
    top_skill = skills[0] if skills else None
    queries: list[str] = []

    def q(*parts: str | None) -> None:
        rendered = " ".join(p for p in parts if p)
        if rendered and rendered not in queries:
            queries.append(rendered)

    # A discovered source profile is the strongest anchor: search the URL
    # itself and the provider's own site for the person unless we already know
    # this IS their profile.
    if fp.source_profile_url:
        q(fp.source_profile_url)
        provider_domain = _provider_domain(fp.source_profile_url, fp.source_provider)
        if provider_domain:
            q(f'site:{provider_domain} "{name}"', company or top_skill)

    if company and location:
        q(f'"{name}"', company, location)
    if company and title and location:
        q(f'"{name}"', company, f'"{title}"', location)
    if company and title and top_skill:
        q(f'"{name}"', company, f'"{title}"', f'"{top_skill}"')
    if company and fp.previous_companies and location:
        q(f'"{name}"', company, fp.previous_companies[0], location)
    if company and location:
        q(f'site:linkedin.com/in/ "{name}"', company, location)
    # Skill-anchored queries: when the employer is unknown, a named skill plus
    # location narrows far better than the name alone.
    if top_skill and location:
        q(f'"{name}" "{top_skill}"', location)
    elif top_skill and title:
        q(f'"{name}" "{top_skill}"', f'"{title}"')
    if title and location:
        q(f'"{name}" "{title}"', location)
    if title and company:
        q(f'site:linkedin.com/in/ "{name}" "{title}"')
    if fp.previous_companies and title:
        q(f'"{name}" {fp.previous_companies[0]} "{title}"')
    # §6 context anchor: when the employer is unknown (or the structured fields
    # yielded fewer than two queries) a distinctive prose term is the only
    # second attribute available. Keep it short: name + one context term +
    # location when we have one.
    terms = context_terms(fp)
    if terms and (not company or len(queries) < 2):
        anchor = terms[0]
        if location:
            q(f'"{name}" "{anchor}"', location)
        else:
            q(f'"{name}" "{anchor}"')
    # Last resort: the strongest attribute we have alongside the name. Never
    # the bare name — if none of the above had a second attribute, `queries`
    # is empty and the caller answers `needs_context`.
    if company:
        q(f'"{name}"', company)
    elif top_skill:
        q(f'"{name}" "{top_skill}"')
    elif location:
        q(f'"{name}"', location)
    elif title:
        q(f'"{name}" "{title}"')
    return queries


# --------------------------------------------------------------------------- #
# Evidence extraction (§13) and scoring (§15)
# --------------------------------------------------------------------------- #


@dataclass
class Evidence:
    type: str
    value: str
    source_url: str
    source_domain: str
    query: str
    confidence: int
    weight: int

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "value": self.value,
            "source_url": self.source_url,
            "source_domain": self.source_domain,
            "query": self.query,
            "confidence": self.confidence,
            "weight": self.weight,
        }


def _contains(text_tokens: set[str], phrase_tokens: set[str]) -> bool:
    """True when every token of `phrase_tokens` appears in `text_tokens`."""
    return bool(phrase_tokens) and phrase_tokens <= text_tokens


def extract_evidence(
    result: NormalizedSearchResult, fp: Fingerprint, weights: dict[str, int]
) -> list[Evidence]:
    """Turn one normalized result into identity evidence (§13, §15).

    Title and snippet are treated as one blob; snippet-only evidence is weaker
    than a corroborated source page (§14), reflected in a lower per-item
    confidence — the weight table stays the single source of magnitudes.
    """
    blob = f"{result.title} {result.snippet or ''}"
    tokens = set(normalize_text(blob).split())
    name = name_tokens(fp.name)
    out: list[Evidence] = []

    # §23: Tier C (aggregator/directory) positive signals are discounted so a
    # handful of mirrored directory pages cannot outvote one real first-party
    # signal. Round down, floor of 1 for a positive signal. Contradictions are
    # handled in `_contradictions` and keep full magnitude — a wrong-person page
    # is a red flag whoever hosts it.
    tier_c = _matches_domain(result.domain, TIER_C_DOMAINS)

    def scaled(weight: int) -> int:
        if weight <= 0 or not tier_c:
            return weight
        return max(1, int(weight * TIER_C_MULTIPLIER))

    def add(etype: str, value: str, weight_key: str) -> None:
        weight = scaled(weights[weight_key])
        out.append(
            Evidence(
                type=etype,
                value=value,
                source_url=result.url,
                source_domain=result.domain,
                query=result.query,
                confidence=min(100, abs(weight) * 4),
                weight=weight,
            )
        )

    if not _contains(tokens, name):
        # The result does not even mention the candidate — it is not evidence
        # about them. Contradictions below are only meaningful when the name
        # matched, so we return before them.
        return out

    # §15 positive signals.
    add("name", fp.name, "name")

    company_tokens = company_phrase_tokens(fp.current_company)
    company_present = _contains(tokens, company_tokens)
    if company_present:
        add("current_company", fp.current_company or "", "current_company")

    # Full-phrase title match only (§15): "Product Control" scores when the page
    # names both words; a page naming just "product" is a fragment and scores
    # nothing — it must neither resolve nor display as title evidence.
    title_tokens = title_phrase_tokens(fp.current_title)
    title_present = _contains(tokens, title_tokens)
    if title_present:
        add("title", fp.current_title or "", "title")

    # §15/§17: the location signal only counts when the page mentions the
    # candidate's location AND does not also name a different market. Query-echo
    # contamination is common — a snippet quotes the query's "Singapore" while
    # the profile is actually "White Plains, New York" — so requiring the
    # absence of another location stops a page from earning the +15 for a
    # location it also contradicts.
    location_present = _contains(tokens, name_tokens(fp.location))
    location_other = _mentions_any_other_location(tokens)
    if location_present and not location_other:
        add("location", fp.location or "", "location")

    matched_previous = False
    for previous in fp.previous_companies:
        if _contains(tokens, org_tokens(previous)):
            add("previous_company", previous, "previous_company")
            matched_previous = True
            break

    if company_present and matched_previous:
        # §15 "matching career history": the one page ties current and past
        # employers together, which a single-employer page cannot.
        add(
            "career_history",
            f"{fp.current_company} / {fp.previous_companies[0]}",
            "career_history",
        )

    # §20/§15: a LinkedIn `/in/` page scores only when some *other* signal on
    # the same result corroborates it. A same-name profile with zero employer,
    # title or location match is a famous namesake as often as the candidate
    # ("Andrew Ng" the AI researcher vs "Andrew Ng" at UBS), so it is recorded
    # as a zero-weight, visible note the modal can name — never as a match.
    corroborated = company_present or title_present or location_present
    if is_professional_profile(result.url):
        if corroborated:
            add("profile_url", canonical_url(result.url), "profile_url")
        else:
            out.append(
                Evidence(
                    type="same_name_profile",
                    value=canonical_url(result.url),
                    source_url=result.url,
                    source_domain=result.domain,
                    query=result.query,
                    confidence=0,
                    weight=0,
                )
            )

    # A result whose URL IS the discovered source profile is the strongest
    # single signal available (§6): it confirms we found the very page the
    # career bot pointed at, whatever its host.
    if fp.source_profile_url and canonical_url(result.url) == canonical_url(
        fp.source_profile_url
    ):
        add("source_profile", canonical_url(result.url), "profile_url")

    # §15 skill signal: each matched named skill is worth a little, capped at
    # the `skills` bucket so three common skills cannot outvote an employer.
    if fp.skills:
        matched_skills = [
            s for s in fp.skills if _contains(tokens, name_tokens(s))
        ]
        if matched_skills:
            capped = min(weights["skills"], 5 * len(matched_skills))
            weight = scaled(capped)
            out.append(
                Evidence(
                    type="skills",
                    value=", ".join(matched_skills),
                    source_url=result.url,
                    source_domain=result.domain,
                    query=result.query,
                    confidence=min(100, weight * 4),
                    weight=weight,
                )
            )

    if any(hint in normalize_text(blob) for hint in _EDU_HINTS):
        add("education", "education mention", "education")

    # §6/§15 context signal: each distinctive prose term present on the page is
    # worth a little, capped at three terms so context can never outvote an
    # employer. This is what lets a page carrying "UBS", "Barclays" and "tenure"
    # outrank a page mentioning only the employer.
    matched_terms = [t for t in context_terms(fp) if t in tokens]
    if matched_terms:
        capped = min(3 * weights["context"], 3 * len(matched_terms))
        weight = scaled(capped)
        out.append(
            Evidence(
                type="context",
                value=", ".join(matched_terms),
                source_url=result.url,
                source_domain=result.domain,
                query=result.query,
                confidence=min(100, weight * 4),
                weight=weight,
            )
        )

    # §15 negative signals — only reachable once the name matched.
    out.extend(_contradictions(tokens, fp, result, weights))
    return out


def _contradictions(
    tokens: set[str],
    fp: Fingerprint,
    result: NormalizedSearchResult,
    weights: dict[str, int],
) -> list[Evidence]:
    """Strong employer / location / role contradictions (§15, §17).

    A contradiction is a result that fits the name but places the candidate
    somewhere the fingerprint says they are not, *without* any supporting
    signal. One page saying a common name works elsewhere is weak evidence of
    a different person; it must not silently raise a cluster's score, and it
    must block the enrichment gate.
    """
    out: list[Evidence] = []

    def add(etype: str, value: str, weight_key: str) -> None:
        out.append(
            Evidence(
                type=etype,
                value=value,
                source_url=result.url,
                source_domain=result.domain,
                query=result.query,
                confidence=min(100, abs(weights[weight_key]) * 3),
                weight=weights[weight_key],
            )
        )

    company_tokens = company_phrase_tokens(fp.current_company)
    company_present = _contains(tokens, company_tokens)
    location_tokens = name_tokens(fp.location)
    location_present = bool(location_tokens) and _contains(tokens, location_tokens)
    previous_present = any(
        _contains(tokens, org_tokens(p)) for p in fp.previous_companies
    )

    # Nothing corroborates this page: at most ONE contradiction fires per
    # result, most-specific first, so a single "wrong person" page cannot
    # stack three negatives onto one cluster and swamp an otherwise good one.
    if company_present or location_present or previous_present:
        return out

    # Employer contradiction: a stated different location and no known
    # employer — the page places this name, emphatically, somewhere else.
    if location_tokens and _mentions_other_location(tokens, fp):
        add("contradiction", "different employer and location", "employer_contradiction")
        return out

    # Location contradiction: the candidate's own location is known and the
    # page places the name in a stated different market.
    if location_tokens and not location_present:
        add("contradiction", "different location", "location_contradiction")
        return out

    # Role contradiction: a full title phrase exists and the page asserts a
    # title nothing like it. A fragment ("product") is not a title, so it must
    # not manufacture a contradiction either.
    title_tokens = title_phrase_tokens(fp.current_title)
    if title_tokens and not _contains(tokens, title_tokens):
        add("contradiction", "different role", "role_contradiction")
        return out

    return out


def _other_location_hits(tokens: set[str]) -> set[str]:
    """The set of known other-market tokens the page names, if any."""
    return tokens & _OTHER_LOCATIONS


def _mentions_any_other_location(tokens: set[str]) -> bool:
    """Whether the page names any known market other than the candidate's.

    Unlike `_mentions_other_location` this does NOT suppress on the presence of
    the candidate's own location: a snippet echoing the query's "Singapore"
    alongside a real "White Plains, New York" names a different market and must
    not earn the location signal (query-echo contamination, §17)."""
    return bool(_other_location_hits(tokens))


def _mentions_other_location(tokens: set[str], fp: Fingerprint) -> bool:
    """Whether the page places the name somewhere else entirely.

    Used by the contradiction path: a page that echoes the candidate's own
    location (query contamination) is not a location contradiction, so the
    candidate's own location suppresses the signal here.

    `tokens` are normalized whole words, so multi-word markets appear here in
    their joined form ("newyork", "whiteplains", "hongkong", "unitedstates").
    """
    known = name_tokens(fp.location) | _SINGAPORE
    return _mentions_any_other_location(tokens) and not (tokens & known)


# --------------------------------------------------------------------------- #
# Clustering (§18) and resolution
# --------------------------------------------------------------------------- #


@dataclass
class Cluster:
    key: str
    urls: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, url: str, evidence: list[Evidence]) -> None:
        """Fold one result's evidence into the cluster, deduplicated.

        The same page is returned by several queries in the progress (§7), and
        the same signal (an employer match, a profile URL) must not be counted
        once per query — that would let three overlapping queries manufacture
        a RESOLVED out of one page. Dedup is on `(type, value, canonical_url)`,
        so two *different* pages corroborating the same employer still each
        count (§14: corroboration across pages is the point).
        """
        if url not in self.urls:
            self.urls.append(url)
        seen = {
            (e.type, e.value, canonical_url(e.source_url)) for e in self.evidence
        }
        for item in evidence:
            mark = (item.type, item.value, canonical_url(item.source_url))
            if mark in seen:
                continue
            seen.add(mark)
            self.evidence.append(item)

    @property
    def score(self) -> int:
        return max(0, min(100, sum(e.weight for e in self.evidence)))

    @property
    def profile_url(self) -> str | None:
        for e in self.evidence:
            if e.type in ("profile_url", "source_profile"):
                return e.value
        return None

    @property
    def corroborated(self) -> bool:
        """§15 — whether anything other than the name and a bare profile
        anchors this cluster. A cluster whose only positive signals are the
        name, a same-name profile and an education mention does not identify
        the candidate; a canonical profile URL must never be offered for it."""
        return any(
            e.type
            in (
                "current_company",
                "title",
                "location",
                "previous_company",
                # §6: a result whose URL IS the candidate's own discovered
                # source profile corroborates by itself.
                "source_profile",
            )
            for e in self.evidence
        )

    def as_dict(self) -> dict:
        return {
            "cluster_id": self.key,
            "urls": self.urls,
            "confidence": self.score,
            "evidence": [e.as_dict() for e in self.evidence],
        }


@dataclass
class Resolution:
    status: str
    confidence: int
    canonical_profile_url: str | None
    current_company: str | None
    current_title: str | None
    location: str | None
    previous_companies: list[str]
    evidence: list[Evidence]
    queries_used: int
    freshness_status: str
    clusters: list[Cluster] = field(default_factory=list)
    enrichment_allowed: bool = False
    contradictions: int = 0
    # §26 Phase 3 first slice: publicly listed emails mined from the result
    # titles/snippets this run already fetched. Free, deterministic, never
    # fetched server-side, and always `verified: False` — see
    # `contact_extract.py`. Empty when the web surfaced no address.
    public_emails: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "canonical_profile_url": self.canonical_profile_url,
            "current_company": self.current_company,
            "current_title": self.current_title,
            "location": self.location,
            "previous_companies": self.previous_companies,
            "evidence": [e.as_dict() for e in self.evidence],
            "queries_used": self.queries_used,
            "freshness_status": self.freshness_status,
            "enrichment_allowed": self.enrichment_allowed,
            "public_emails": self.public_emails,
        }


def _status_for(confidence: int, thresholds: tuple[int, int]) -> str:
    resolved_min, probable_min = thresholds
    if confidence >= resolved_min:
        return "resolved"
    if confidence >= probable_min:
        return "probable"
    return "unresolved"


def _freshness(fp: Fingerprint, clusters: list[Cluster]) -> str:
    """§21 — compare the record's employer with what the web now shows.

    Returns `current` when some page corroborates the recorded employer,
    `possible_change` only when a page actively contradicts the employer, and
    `unknown` otherwise. `unknown` is the honest default: a same-name profile,
    an education mention or a URL is not evidence that the employer changed,
    and a location/role mismatch is a different *person*, not a changed job —
    it already blocks the gate, so it must not masquerade as an employment
    change. The previous implementation returned `possible_change` whenever
    the evidence carried any token beyond the name — which a bare profile URL
    always does, so a famous namesake read as "Employment may have changed".
    """
    if not clusters:
        return "unknown"
    evidence = [e for cluster in clusters for e in cluster.evidence]
    if any(e.type == "current_company" for e in evidence):
        return "current"
    if any(
        e.type == "contradiction" and "employer" in e.value for e in evidence
    ):
        return "possible_change"
    return "unknown"


def _safety_gate(
    status: str,
    clusters: list[Cluster],
    contradictions: int,
    name_only: bool,
) -> bool:
    """§17 hard safety conditions. `True` only when automatic enrichment may
    proceed — Phase 1 never enriches, but the flag is what Phase 3 reads, and
    a test asserts each condition independently."""
    if REQUIRE_RESOLVED_IDENTITY and status != "resolved":
        return False
    if contradictions > 0:
        return False
    if name_only:
        return False
    # Multiple high-scoring identities remaining (§17).
    high = [c for c in clusters if c.score >= PROBABLE_MIN]
    # Distinct person-scoped identities at or above the probable floor are two
    # different people: a LinkedIn `/in/` URL and an aggregator profile URL are
    # both person-scoped (§18). Two pages on one host are one person.
    distinct = {
        c.key
        for c in high
        if c.key.startswith("li:") or c.key.startswith("agg:")
    }
    if len(distinct) >= 2:
        return False
    return True


def _name_only(clusters: list[Cluster]) -> bool:
    top = max(clusters, key=lambda c: c.score, default=None)
    if top is None:
        return True
    return all(e.type == "name" for e in top.evidence)


# --------------------------------------------------------------------------- #
# Execution (§9, §32)
# --------------------------------------------------------------------------- #


async def resolve(
    fp: Fingerprint,
    provider: WebSearchProvider,
    *,
    mode: str = "normal",
    weights: dict[str, int] | None = None,
    thresholds: tuple[int, int] | None = None,
    budget: dict[str, int] | None = None,
) -> Resolution:
    """Run the progressive search and resolve the identity (§9, §32).

    Queries execute **sequentially**, most specific first, and the run stops
    the moment confidence reaches `EARLY_STOP_MIN` with no contradiction
    (§32). Budget caps the number of Serper calls: `normal` 3, `refresh` 5,
    `deep` 8 (§31).
    """
    weights = weights or WEIGHTS
    thresholds = thresholds or (RESOLVED_MIN, PROBABLE_MIN)
    budget = budget or QUERY_BUDGET

    limit = budget.get(mode, budget.get("normal", 3))
    queries = build_queries(fp)[:limit]

    if not queries:
        # §4: a name-only fingerprint has nothing to anchor a query on. Return
        # without a single provider call — the route maps this to
        # `needs_context` and nothing is persisted.
        return Resolution(
            status="needs_context",
            confidence=0,
            canonical_profile_url=None,
            current_company=fp.current_company,
            current_title=fp.current_title,
            location=fp.location,
            previous_companies=list(fp.previous_companies),
            evidence=[],
            queries_used=0,
            freshness_status="unknown",
            clusters=[],
            enrichment_allowed=False,
            contradictions=0,
            public_emails=[],
        )

    clusters: dict[str, Cluster] = {}
    contradictions = 0
    queries_used = 0

    # §26: the result-page texts already fetched, index-aligned with their URLs,
    # so the public-email pass at the end needs no new network call and no
    # stored raw response. `title + snippet` is the whole blob a recruiter could
    # see in the search UI — nothing is fetched server-side.
    page_texts: list[str] = []
    page_urls: list[str] = []

    # §32 ambiguity early stop: when the run is stably ambiguous — the leader is
    # below RESOLVED but two distinct person-scoped identities already sit at
    # PROBABLE or above — more queries only spend Serper quota to learn what the
    # first few already showed. We stop once the leader's cluster is the same as
    # the previous query's, provided at least three queries ran (so a single
    # lucky page cannot pre-empt the run).
    AMBIGUITY_MIN_QUERIES = 3
    prev_leader: str | None = None

    for query in queries:
        result_set = await provider.search(SearchQuery(query=query))
        queries_used += 1
        for result in result_set.results:
            # Record the page text for the §26 public-email pass regardless of
            # whether the page carried identity evidence — an address on a page
            # that did not corroborate the identity is still a public listing.
            page_texts.append(f"{result.title} {result.snippet or ''}")
            page_urls.append(result.url)
            evidence = extract_evidence(result, fp, weights)
            if not evidence:
                continue
            key = cluster_key(result.url)
            cluster = clusters.setdefault(key, Cluster(key=key))
            cluster.add(result.url, evidence)
            contradictions += sum(1 for e in evidence if e.type == "contradiction")

        # §32 early stop: sufficient confidence AND no major contradiction.
        if clusters:
            best = max(c.score for c in clusters.values())
            if best >= EARLY_STOP_MIN and contradictions == 0:
                break

            # Stable ambiguity: the best cluster is still below RESOLVED, yet two
            # or more distinct person-scoped identities already clear the
            # PROBABLE floor. If the leader has not changed from the previous
            # query and we are at least `AMBIGUITY_MIN_QUERIES` in, the answer
            # will not improve — stop and record the queries actually used.
            leader = max(clusters.values(), key=lambda c: c.score)
            distinct_high = {
                c.key
                for c in clusters.values()
                if c.score >= PROBABLE_MIN
                and (c.key.startswith("li:") or c.key.startswith("agg:"))
            }
            if (
                queries_used >= AMBIGUITY_MIN_QUERIES
                and best < EARLY_STOP_MIN
                and len(distinct_high) >= 2
                and leader.key == prev_leader
            ):
                break
            prev_leader = leader.key

    ordered = sorted(clusters.values(), key=lambda c: c.score, reverse=True)
    top = ordered[0] if ordered else None
    confidence = top.score if top else 0
    status = _status_for(confidence, thresholds)
    freshness = _freshness(fp, ordered)

    # §17 ambiguity: two or more DISTINCT person-scoped identities sitting at
    # PROBABLE or above means we do not know *which* person this is. The score
    # is real (it is the best candidate's), but reporting RESOLVED is a lie — so
    # downgrade to `probable`, withhold the canonical profile, and drop the
    # freshness verdict (a job-change warning is meaningless when the subject is
    # ambiguous). Enrichment stays blocked via the status check in the gate.
    distinct_people = {
        c.key
        for c in ordered
        if c.score >= PROBABLE_MIN and (c.key.startswith("li:") or c.key.startswith("agg:"))
    }
    ambiguous = len(distinct_people) >= 2
    if ambiguous and status == "resolved":
        status = "probable"
        freshness = "unknown"

    evidence_flat = list(top.evidence) if top else []
    # §15: a canonical profile is only trustworthy when the top cluster carries
    # employer/title/location corroboration on the same page, the identity is
    # not ambiguous, and the cluster actually clears the probable floor — a
    # corroborated same-name page below PROBABLE is still a coin flip and must
    # not be presented as "Professional profile found". The one exception is a
    # URL that IS the candidate's own discovered source profile (§6): finding
    # that very page is the strongest single signal and stands on its own.
    source_profile_found = top is not None and any(
        e.type == "source_profile" for e in top.evidence
    )
    offerable = top is not None and top.corroborated and (
        confidence >= PROBABLE_MIN or source_profile_found
    )
    canonical = top.profile_url if (offerable and not ambiguous) else None
    return Resolution(
        status=status,
        confidence=confidence,
        canonical_profile_url=canonical,
        current_company=fp.current_company,
        current_title=fp.current_title,
        location=fp.location,
        previous_companies=list(fp.previous_companies),
        evidence=evidence_flat,
        queries_used=queries_used,
        freshness_status=freshness,
        clusters=ordered,
        enrichment_allowed=_safety_gate(
            status, ordered, contradictions, _name_only(ordered)
        ),
        contradictions=contradictions,
        public_emails=_extract_public_emails(page_texts, page_urls),
    )


# --------------------------------------------------------------------------- #
# Cache TTLs (§34)
# --------------------------------------------------------------------------- #

# §34/§61: resolved 30 days, probable (ambiguous) 3 days, unresolved 7 days.
CACHE_TTL_DAYS: dict[str, int] = {"resolved": 30, "probable": 3, "unresolved": 7}


def expires_at(status: str, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.UTC)
    days = CACHE_TTL_DAYS.get(status, CACHE_TTL_DAYS["unresolved"])
    return now + dt.timedelta(days=days)
