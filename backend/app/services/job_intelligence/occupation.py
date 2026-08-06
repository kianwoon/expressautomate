"""Module 4 — match the work to a MOM occupation for salary benchmarking.

A three-step hybrid pipeline, mirroring how the rest of the engine composes
LLM judgement with deterministic retrieval:

1. **Extract a structured work profile** (LLM): distil the job order into the
   facets that distinguish occupations — canonical role, seniority, people
   management, industry, and a proportional weighting of activity areas. This
   is cleaner signal for matching than the raw job ad.

2. **Semantic search** (pgvector): embed the profile text and retrieve the
   nearest MOM occupation titles by cosine similarity. Reads the global
   `mom_occupations` reference library.

3. **Re-rank and validate** (LLM): given the top-k candidate occupations, pick
   the one that best represents the work and explain why. A small, controlled
   decision space makes the model more consistent than an open-ended match.

Degrades to `None` (no match) when embeddings are unconfigured, the reference
library is empty, or the model declines to commit — the rest of the analysis is
still useful without a benchmark, so a missing match fails soft rather than
failing the whole run.

Like the other three stages, the LLM seam is threaded so a `FakeLLM` queued
with two responses (profile + pick) tests the whole stage without a real call.
The semantic-search step takes an optional session: None in tests or when the
library is absent skips retrieval and yields no candidates.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.services.job_intelligence.render import understanding_text
from app.services.job_intelligence.schema import (
    JDUnderstanding,
    OccupationMatch,
    OccupationProfile,
    json_schema,
)
from app.services.job_intelligence.understand import model
from app.services.llm.client import LLMResult, complete_json
from app.services.llm.embeddings import embed_one

log = get_logger(__name__)

# allow-hardcode: prompts, not configuration.
_PROFILE_PROMPT = """You are a labour-market analyst. Read this job order and distil it into a
structured OCCUPATION PROFILE that can be matched against a standard list of
Singapore occupations.

Rules:
- `occupation` is the single most accurate standard role name for this work
  (e.g. "Software Developer", "Human Resource Officer"). Prefer the generic
  role over a company-specific title ("Senior Java Ninja" -> "Software
  Developer").
- `functions` is an object mapping each major activity area to the percentage
  of the role it represents (integers, ideally summing to 100). Name 2-5 areas.
- `seniority` is one of: Entry, Junior, Mid, Senior, Lead, Manager, Director,
  Executive. Pick the closest.
- `people_management` is true only if the role's core duty includes managing a
  team (not merely coordinating peers).
- `industry` is the sector, or "General" if the role is cross-sector.
- Ignore "[redacted]" markers — they are withheld requirements, not skills.

Return JSON matching this schema:
{schema}

THE WORK:
{understanding}

THE JOB ORDER (for context):
{context}
"""

# allow-hardcode: a prompt, not configuration.
_PICK_PROMPT = """You are a labour-market analyst. Below is the actual job order, a structured
occupation profile, and a list of candidate standard occupations ranked by a
weak semantic search. Your job is to pick the ONE occupation that genuinely
represents this work — or reject all of them.

Rules:
- Read the JOB ORDER first. The candidate list comes from a fuzzy title search
  and frequently contains near-misses that share a word but not the work
  (e.g. "account executive" in sales vs accounting). Reason about what the
  person actually does day to day, not about word overlap in the titles.
- Choose from the listed candidates only. `title` must be exactly one of the
  candidate titles. Do not invent or paraphrase a title.
- If NONE of the candidates is a genuine fit for the work described, choose the
  closest one anyway but set `confidence` below 0.5 — a low confidence signals
  "no good match" to the system, which will then hide the benchmark rather
  than show a misleading one.
- `confidence` (0.0-1.0) is how well the chosen title fits the ACTUAL WORK,
  not how similar the strings are. A sales role matched to an accounting title
  is confidence 0.1, not 0.7, even if the words overlap.
- `rationale` is ONE sentence: name the closest candidate and say why it fits
  or why it does not.

Return JSON matching this schema:
{schema}

JOB ORDER:
{context}

PROFILE:
{profile}

CANDIDATES (title | gross P25 / median / P75, monthly SGD):
{candidates}
"""


def build_profile_prompt(context: str, understanding_text_block: str) -> str:
    """Separate from `extract_occupation_profile` for prompt-only testing."""
    return _PROFILE_PROMPT.format(
        schema=json_schema()["occupation_profile"],
        understanding=understanding_text_block,
        context=context,
    )


def build_pick_prompt(
    context: str, profile: OccupationProfile, candidates: list[dict]
) -> str:
    """Separate from `rerank_occupation` for prompt-only testing."""
    lines = []
    for c in candidates:
        lines.append(
            f"- {c['title']} | {c['gross_p25']} / {c['gross_p50']} / {c['gross_p75']}"
        )
    return _PICK_PROMPT.format(
        schema=json_schema()["occupation_pick"],
        context=context,
        profile=_profile_text(profile),
        candidates="\n".join(lines),
    )


def _profile_text(profile: OccupationProfile) -> str:
    """A profile as a readable block the re-rank prompt reads."""
    lines = [
        f"Occupation: {profile.occupation}",
        f"Seniority: {profile.seniority}",
        f"People management: {'yes' if profile.people_management else 'no'}",
        f"Industry: {profile.industry}",
    ]
    if profile.functions:
        parts = [f"{k} {v}%" for k, v in profile.functions.items()]
        lines.append("Functions: " + "; ".join(parts))
    return "\n".join(lines)


def _profile_for_embedding(profile: OccupationProfile) -> str:
    """The text embedded for semantic search against occupation titles.

    The occupation name leads because it is the strongest signal; seniority,
    people management, and the activity areas follow so a role whose title is
    generic ("Manager") can still be disambiguated by what it manages. The
    numeric percentages are dropped — they convey emphasis to a reader but add
    noise to a vector comparison against short title strings.
    """
    parts = [profile.occupation, profile.seniority]
    if profile.people_management:
        parts.append("people management")
    if profile.industry and profile.industry.lower() != "general":
        parts.append(profile.industry)
    parts.extend(profile.functions.keys())
    return ", ".join(p for p in parts if p)


async def extract_occupation_profile(
    context: str, understanding: JDUnderstanding, *, llm=None
) -> tuple[OccupationProfile, LLMResult]:
    """Step 1: distil the work into a structured occupation profile (LLM)."""
    resolve = llm or complete_json
    prompt = build_profile_prompt(context, understanding_text(understanding))
    result = await resolve(
        prompt,
        model=model(),
        schema=None,
        base_url=settings.CEREBRAS_BASE_URL,
        api_key=settings.CEREBRAS_API_KEY,
        extra_body={
            "max_tokens": settings.EXTRACTION_MAX_TOKENS,
            "reasoning_effort": settings.EXTRACTION_REASONING_EFFORT_FAST,
        },
    )
    return OccupationProfile.model_validate(result.data), result


async def search_occupations(
    session: AsyncSession | None,
    profile: OccupationProfile,
    *,
    k: int = 10,
) -> list[dict]:
    """Step 2: semantic nearest-neighbour search against `mom_occupations`.

    Returns a list of candidate rows (title, year, the six wage percentiles,
    cosine similarity) ordered by similarity descending. Empty when the session
    is None (tests / no DB), embeddings are unconfigured, or the library has no
    vectors yet — the graceful-degradation contract the caller depends on.

    Raw SQL with the `<=>` operator, mirroring `semantic_neighbors`: pgvector's
    cosine distance is an extension type, and the bound `[…]::vector` literal
    is the reliable way to pass a Python list. `1 - (embedding <=> :q::vector)`
    is cosine similarity because both vectors are L2-normalised.
    """
    if session is None or not settings.embedding_configured():
        return []

    query_vector = await embed_one(_profile_for_embedding(profile))
    if not query_vector or k <= 0:
        return []

    # allow-hardcode: raw SQL selecting the model's wage columns by name. The
    # vector literal is built the same way `semantic_neighbors` builds it.
    # The vector literal is built entirely from str(float(...)) — digits, dots,
    # minus, exponent, commas, brackets — so it is injection-safe to inline.
    # Binding it as a :param fails: SQLAlchemy's text() parser misreads the
    # Postgres `::vector` cast, and asyncpg rejects the `:` it leaves behind.
    # Inlining the literal sidesteps both, the same way pgvector's own docs show.
    vector_literal = "[" + ",".join(str(float(v)) for v in query_vector) + "]"
    # allow-hardcode: raw SQL selecting the wage columns by name; the vector
    # literal is interpolated (safe — see above), the limit is bound.
    sql = text(f"""
        SELECT title, year,
               gross_p25, gross_p50, gross_p75,
               basic_p25, basic_p50, basic_p75,
               1 - (embedding <=> '{vector_literal}'::vector) AS similarity
        FROM mom_occupations
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> '{vector_literal}'::vector
        LIMIT :k
    """)
    rows = (
        await session.execute(
            sql, {"k": k}
        )
    ).mappings().all()
    return [
        {
            "title": r["title"],
            "year": int(r["year"]),
            "gross_p25": float(r["gross_p25"]),
            "gross_p50": float(r["gross_p50"]),
            "gross_p75": float(r["gross_p75"]),
            "basic_p25": float(r["basic_p25"]),
            "basic_p50": float(r["basic_p50"]),
            "basic_p75": float(r["basic_p75"]),
            "similarity": float(r["similarity"]),
        }
        for r in rows
    ]


# A match the model is less than this confident about is suppressed: no
# benchmark is shown rather than a misleading one. The semantic search returns
# near-misses that share a word but not the work (sales "account executive" vs
# accounting), and a benchmark against the wrong occupation misleads a
# recruiter into the wrong salary expectation. Below the floor, the chart hides.
# allow-hardcode: a quality threshold, not configuration.
_MIN_CONFIDENCE = 0.5


async def rerank_occupation(
    context: str,
    profile: OccupationProfile,
    candidates: list[dict],
    *,
    llm=None,
) -> tuple[OccupationMatch, LLMResult] | None:
    """Step 3: pick the best occupation from the candidates (LLM).

    Returns the chosen `OccupationMatch` (wages filled from the candidate row,
    never the model) plus the LLM result. Returns None when the model's chosen
    title does not appear among the candidates, or when the model's confidence
    is below the quality floor — both are refusals that should not become a
    benchmark, because a benchmark against the wrong occupation is worse than
    no benchmark at all.
    """
    if not candidates:
        return None

    resolve = llm or complete_json
    prompt = build_pick_prompt(context, profile, candidates)
    result = await resolve(
        prompt,
        model=model(),
        schema=None,
        base_url=settings.CEREBRAS_BASE_URL,
        api_key=settings.CEREBRAS_API_KEY,
        extra_body={
            "max_tokens": settings.EXTRACTION_MAX_TOKENS,
            "reasoning_effort": settings.EXTRACTION_REASONING_EFFORT_FAST,
        },
    )

    picked = result.data
    title = (picked.get("title") or "").strip()
    confidence = float(picked.get("confidence") or 0.0)

    # The model is told to choose from the list only; a title it invented (or
    # misspelled) is a non-match rather than a benchmark we cannot ground.
    by_title = {c["title"]: c for c in candidates}
    # Case-insensitive match: the model may capitalise differently than the
    # survey's lower-cased title.
    match = next(
        (c for t, c in by_title.items() if t.lower() == title.lower()), None
    )
    if match is None:
        log.info("occupation_rerank_unmatched", picked_title=title)
        return None

    if confidence < _MIN_CONFIDENCE:
        log.info(
            "occupation_rerank_low_confidence",
            picked_title=title,
            confidence=confidence,
        )
        return None

    return (
        OccupationMatch(
            title=match["title"],
            year=match["year"],
            gross_p25=match["gross_p25"],
            gross_p50=match["gross_p50"],
            gross_p75=match["gross_p75"],
            basic_p25=match["basic_p25"],
            basic_p50=match["basic_p50"],
            basic_p75=match["basic_p75"],
            similarity=match["similarity"],
            confidence=float(picked.get("confidence") or 0.0),
            rationale=(picked.get("rationale") or "").strip(),
        ),
        result,
    )


async def classify_occupation(
    context: str,
    understanding: JDUnderstanding,
    *,
    session: AsyncSession | None = None,
    llm=None,
) -> tuple[OccupationMatch | None, LLMResult | None]:
    """Run all three occupation steps, returning the match (or None).

    The orchestrator's convenience: extract → search → re-rank in one call,
    folding the two LLM results so the engine can sum their cost. Returns
    `(None, None)` when the search yields no candidates — a clean degradation
    that leaves the rest of the analysis intact.
    """
    profile, r1 = await extract_occupation_profile(context, understanding, llm=llm)
    candidates = await search_occupations(session, profile)
    if not candidates:
        return None, r1
    picked = await rerank_occupation(context, profile, candidates, llm=llm)
    if picked is None:
        return None, r1
    match, r2 = picked
    return match, _coalesce(r1, r2)


def _coalesce(r1: LLMResult, r2: LLMResult) -> LLMResult:
    """Sum two LLM results' token/latency counters into one.

    The engine sums all stage costs into one `JobIntelligenceStats`, so the
    two occupation calls (profile + re-rank) are folded here into a single
    result the engine can add alongside the other three stages.
    """
    return LLMResult(
        data={},
        model=r2.model,
        prompt_tokens=_nz(r1.prompt_tokens) + _nz(r2.prompt_tokens),
        completion_tokens=_nz(r1.completion_tokens) + _nz(r2.completion_tokens),
        latency_ms=_nz(r1.latency_ms) + _nz(r2.latency_ms),
        raw={},
    )


def _nz(v: int | None) -> int:
    return v or 0
