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
    "employer_contradiction": -30,
    "location_contradiction": -25,
    "role_contradiction": -20,
}

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

    A LinkedIn profile URL is person-scoped, so it clusters on its own. Every
    other URL clusters by host: a company bio and a conference page on the
    same domain are one identity, two pages."""
    canon = canonical_url(url)
    if is_professional_profile(canon):
        return f"li:{canon}"
    from urllib.parse import urlsplit

    return f"dom:{(urlsplit(canon).hostname or '') if canon else ''}"


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

    def add(etype: str, value: str, weight_key: str) -> None:
        out.append(
            Evidence(
                type=etype,
                value=value,
                source_url=result.url,
                source_domain=result.domain,
                query=result.query,
                confidence=min(100, abs(weights[weight_key]) * 4),
                weight=weights[weight_key],
            )
        )

    if not _contains(tokens, name):
        # The result does not even mention the candidate — it is not evidence
        # about them. Contradictions below are only meaningful when the name
        # matched, so we return before them.
        return out

    # §15 positive signals.
    add("name", fp.name, "name")

    company_tokens = org_tokens(fp.current_company)
    company_present = _contains(tokens, company_tokens)
    if company_present:
        add("current_company", fp.current_company or "", "current_company")

    title_tokens = name_tokens(fp.current_title) - _STOPWORDS
    title_present = bool(title_tokens) and _contains(tokens, title_tokens)
    if title_present:
        add("title", fp.current_title or "", "title")

    location_present = _contains(tokens, name_tokens(fp.location))
    if location_present:
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
            out.append(
                Evidence(
                    type="skills",
                    value=", ".join(matched_skills),
                    source_url=result.url,
                    source_domain=result.domain,
                    query=result.query,
                    confidence=min(100, capped * 4),
                    weight=capped,
                )
            )

    if any(hint in normalize_text(blob) for hint in _EDU_HINTS):
        add("education", "education mention", "education")

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

    company_tokens = org_tokens(fp.current_company)
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

    # Role contradiction: a title signal exists and the page asserts a title
    # nothing like it.
    title_tokens = name_tokens(fp.current_title) - _STOPWORDS
    if title_tokens and not _contains(tokens, title_tokens):
        add("contradiction", "different role", "role_contradiction")
        return out

    return out


def _mentions_other_location(tokens: set[str], fp: Fingerprint) -> bool:
    """Whether the page names a location that is not the candidate's."""
    known = name_tokens(fp.location) | _SINGAPORE
    # A tiny, high-precision list: an explicit different country is the signal
    # we trust. This is deliberately not a global gazetteer.
    other = {
        "london",
        "hongkong",
        "newyork",
        "tokyo",
        "sydney",
        "dubai",
        "shanghai",
        "mumbai",
        "bangalore",
        "paris",
        "berlin",
    }
    return bool(tokens & other) and not (tokens & known)


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
    # Distinct LinkedIn identities at or above the probable floor are two
    # different people; two pages on one domain are one person (§18).
    distinct = {c.key for c in high if c.key.startswith("li:")}
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
        )

    clusters: dict[str, Cluster] = {}
    contradictions = 0
    queries_used = 0

    for query in queries:
        result_set = await provider.search(SearchQuery(query=query))
        queries_used += 1
        for result in result_set.results:
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

    ordered = sorted(clusters.values(), key=lambda c: c.score, reverse=True)
    top = ordered[0] if ordered else None
    confidence = top.score if top else 0
    status = _status_for(confidence, thresholds)
    freshness = _freshness(fp, ordered)

    evidence_flat = list(top.evidence) if top else []
    # §15: a canonical profile is only trustworthy when the top cluster carries
    # employer/title/location corroboration on the same page. Otherwise a
    # same-name namesake would be presented as "Professional profile found".
    canonical = top.profile_url if top and top.corroborated else None
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
