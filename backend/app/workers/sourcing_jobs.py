"""The arq job that ranks an agency's candidates against one job order.

Its own module rather than another function in `app.workers.jobs`, for the
reason `cv_jobs.py` and `import_jobs.py` both give: that file is at the repo's
1500-line ceiling, and a sourcing run shares nothing with mail ingestion but
the queue it arrives on.

**A run is a record, not a live query.** Everything it decided is written
down — the score, the components behind it, the explanation and the quote
that supports it — so re-reading the run next month shows what the recruiter
was actually shown, not what the same arithmetic would say over today's data.
That is the whole reason `sourcing_runs` exists as a table.

**The job carries its tenant**, like every other job here. Background work has
no request and therefore no session tenant, and a job naming a mismatched
(tenant, run) pair reads no row under the tenant policy and quietly does
nothing.

**A run is bounded in wall clock and in attempts**, exactly as an import is.
`settings.py` registers this function with `SOURCING_JOB_TIMEOUT_SECONDS`; a
timed-out run is left at `running`, which `rescan_stuck` picks up.
`sourcing_runs.attempts` is spent in the same statement that claims the row,
so a job order that deterministically crashes the scorer parks itself in
`failed` rather than being re-enqueued for ever, one worker slot and one
model call per sweep.

**Candidates the scorer could not score are dropped, not stored as zero.**
`score_candidate` returns `None` when no component had anything to compare,
and `sourcing_matches.score` is NOT NULL — so the insert would fail even if
the meaning did not. It does not: a `0` in that column reads as "unsuitable"
to every reader of the shortlist, and the truth is that we know nothing about
this person yet.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.opportunity import Opportunity
from app.models.opportunity_code import OpportunityCode
from app.models.sourcing import SourcingRun
from app.services.llm.embeddings import embed_one
from app.services.sourcing.eligible import eligible_candidates
from app.services.sourcing.embed import opportunity_text_for_embedding
from app.services.sourcing.explain import MatchCandidate, explain_matches
from app.services.sourcing.persist import (
    candidate_sexes,
    load_scoring_inputs,
    parsed_text_keys,
    record_matches,
    semantic_neighbors,
    serialize_components,
)
from app.services.sourcing.preference import implied_sex
from app.services.sourcing.score import Component, score_candidate
from app.services.storage.r2 import R2BodyStore

log = get_logger(__name__)

# The states a run may legitimately start from. `running` is included
# deliberately: a worker killed mid-run leaves the row there and
# `rescan_stuck` re-enqueues exactly this job for it, so accepting only
# `pending` would strand the run for ever. `done` and `failed` are answers —
# replaying the job on either must change nothing.
_RESUMABLE = (SourcingRun.PENDING, SourcingRun.RUNNING)


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


def _today() -> date:
    """Today in UTC, passed into the scorer rather than read inside it.

    `score_candidate` requires it for exactly this reason: tenure and recency
    are measured against a date, and a hidden `date.today()` would make a run
    unreproducible in the one module built to be reproducible.
    """
    return datetime.now(UTC).date()


def _semantic_weight() -> Decimal:
    """The configured semantic weight, as a Decimal for the rescue arithmetic."""
    return Decimal(str(settings.SOURCING_WEIGHT_SEMANTIC))


def _decimal_floor(value: float) -> Decimal:
    """A similarity rounded to the scorer's agreed precision.

    The rescue path synthesises a score from similarity, and it must land on
    the same number of decimal places the scorer itself uses — otherwise a
    recruiter reading two runs side by side would see a rescued candidate's
    score carry more digits than a scored one, which reads as a different kind
    of number rather than the same one.
    """
    quantum = Decimal(1).scaleb(-settings.SOURCING_SCORE_DECIMAL_PLACES)
    return Decimal(str(value)).quantize(quantum)


async def _semantic_scores(
    session,
    tenant: uuid.UUID,
    opportunity,
    candidate_ids: list[uuid.UUID] | None = None,
    codes: list | None = None,
) -> tuple[dict[uuid.UUID, float], list]:
    """Candidate → cosine similarity for this job order, plus the codes loaded.

    The whole semantic stage in one function, so `run_sourcing` reads as the
    pipeline it is. Embeds the job order once (redacted of protected-attribute
    codes), asks pgvector for the nearest CVs among the eligible roster, and
    returns the similarity map plus the `OpportunityCode` rows it loaded for
    redaction — the same rows `explain_matches` needs later, so the run loads
    them once rather than twice.

    `codes` are the `OpportunityCode` rows the caller already loaded for the sex
    prefilter (see `run_sourcing`). When passed, this function reuses them and
    does not re-fetch; when omitted it loads them itself, so the helper stays
    usable on its own. The rows are needed here for redaction regardless.

    Returns empty similarities — not raises — when embeddings are not
    configured, when the JD has no text to embed, or when no candidate has an
    embedding yet. Every one of those is the deployment that has not opted in,
    and the run proceeds on the six structured components unchanged.

    `candidate_ids` scopes the ANN search to the eligible roster, so the rescue
    path never sees a neighbour that is archived, placed, or already submitted
    to this client — those are filtered before the vector query, not after.
    """
    if codes is None:
        codes = list(
            (
                await session.execute(
                    select(OpportunityCode).where(OpportunityCode.opportunity_id == opportunity.id)
                )
            ).scalars()
        )
    if not settings.embedding_configured():
        return {}, codes
    jd_text, _removed = opportunity_text_for_embedding(opportunity, codes)
    if not jd_text.strip():
        return {}, codes
    try:
        query_vector = await embed_one(jd_text)
    except Exception:
        # Classed, not fatal: a provider stall during the embedding call must
        # not abort a run that can still rank on six structured components.
        # Logged at warning so a persistent problem is visible.
        log.warning("sourcing_jd_embed_failed", exc_info=True)
        return {}, codes
    if not query_vector:
        return {}, codes
    neighbours = await semantic_neighbors(
        session,
        tenant_id=tenant,
        query_vector=query_vector,
        candidate_ids=candidate_ids,
        k=settings.SOURCING_SEMANTIC_RECALL_K,
    )
    return neighbours, codes


async def run_sourcing(
    ctx,
    *,
    tenant_id: str,
    opportunity_id: str,
    run_id: str,
    client_id: str | None = None,
) -> None:
    """Rank the eligible candidates for one job order and store the result.

    `client_id` is decided by the route that created the run, not here. It was
    inferred here once, from `client_mentions` on the source email, with a nil
    UUID standing in when it could not be — which silently disabled the
    already-submitted exclusion and left no trace that it had. Resolution now
    happens once, at enqueue, and is written to `sourcing_runs.client_id`.

    The argument is optional because `rescan_stuck` re-enqueues a stranded run
    from the sweep resolver, which carries routing ids only; that path falls
    back to the column, which is the second reason the column exists. Passing
    it explicitly still matters: the enqueue path should not depend on its own
    write having landed before the worker reads it.

    Failure discipline mirrors `run_candidate_import`. The row moves to
    `running` before the long operation, because arq only reschedules on
    `Retry` and nothing here raises one: an infrastructure failure is a
    permanently failed job and `rescan_stuck` re-enqueues the row once the
    outage ends. Leaving it at `pending` across the run would instead let the
    sweep start the same run twice, concurrently, and the second writer would
    collide with the first on `uq_sourcing_matches_once_per_run` after both
    had paid for a model call.
    """
    tenant = uuid.UUID(tenant_id)
    opportunity_key = uuid.UUID(opportunity_id)
    record = uuid.UUID(run_id)
    client = uuid.UUID(client_id) if client_id else None

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(select(SourcingRun).where(SourcingRun.id == record))
        ).scalar_one_or_none()
        if row is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info("sourcing_skipped_unknown_run", sourcing_run_id=run_id)
            return
        if row.state not in _RESUMABLE:
            log.info(
                "sourcing_skipped_already_answered",
                sourcing_run_id=run_id,
                state=row.state,
            )
            return

        # The sweep re-enqueues with routing ids only, so fall back to what
        # the route wrote on the row. `None` on both sides is a real answer,
        # not a missing one: this job order resolved to no client, and
        # `client_unresolved_reason` on the same row says so.
        if client is None:
            client = row.client_id

        # The claim is a conditional UPDATE, not the read above followed by a
        # write. The read is only good enough to log with: between it and the
        # write another worker — `rescan_stuck` re-enqueued this run while the
        # first attempt was still going — can claim the same row, and a blind
        # write would let both proceed to score, explain and insert. Restating
        # the state in the WHERE clause makes the check and the write one
        # indivisible statement; whoever loses simply matches no row.
        #
        # The attempt is spent in that same statement. Counting at the end
        # instead would count nothing on exactly the runs this bounds — a
        # crash inside the scorer never reaches an end — and a job order that
        # deterministically crashes would be re-enqueued by `rescan_stuck` for
        # ever, buying a model call each time and telling nobody.
        claimed = (
            await session.execute(
                update(SourcingRun)
                .where(
                    SourcingRun.id == record,
                    SourcingRun.state.in_(_RESUMABLE),
                )
                .values(state=SourcingRun.RUNNING, attempts=SourcingRun.attempts + 1)
                .returning(SourcingRun.attempts)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            log.info("sourcing_skipped_claimed_elsewhere", sourcing_run_id=run_id)
            return
        (attempts,) = claimed
        await session.commit()

    if attempts > settings.SOURCING_MAX_ATTEMPTS:
        # Terminal, so `rescan_stuck` stops seeing it. The row is claimed
        # first and refused second on purpose: leaving it at `pending` while
        # refusing would let the sweep pick it up again on the next pass and
        # discover the same thing, which is the loop rather than the end of it.
        log.warning("sourcing_attempts_exhausted", sourcing_run_id=run_id, attempts=attempts)
        await _fail(tenant, record)
        return

    async with tenant_session(tenant) as session:
        opportunity = (
            await session.execute(select(Opportunity).where(Opportunity.id == opportunity_key))
        ).scalar_one_or_none()
        if opportunity is None:
            # The job order was deleted between the request and the worker.
            # There is nothing left to rank against, and no useful run to
            # store — `failed` is what the panel should show.
            log.info("sourcing_opportunity_missing", sourcing_run_id=run_id)
            await _fail(tenant, record)
            return

        candidate_ids = await eligible_candidates(session, tenant_id=tenant, client_id=client)

        # Load the opportunity's shorthand codes once here, ahead of the sex
        # prefilter, so the same rows feed both the narrowing decision and the
        # redaction `_semantic_scores` and `explain_matches` do later — fetched
        # once per run, never twice.
        codes = list(
            (
                await session.execute(
                    select(OpportunityCode).where(OpportunityCode.opportunity_id == opportunity.id)
                )
            ).scalars()
        )

        # --- Client sex preference: narrow the pool, don't record a
        # requirement. The client's C/F or O/F is a preference, not a legal
        # occupational requirement, so it is honoured in who the shortlist
        # contains rather than written to `opportunities.sex_requirement`
        # (which exists for genuine occupational requirements and is human-set
        # with a written reason). Missing sex on a candidate is not a
        # disqualification — see `_narrow_by_sex`.
        prefilter_sex: str | None = None
        prefilter_dropped = 0
        implied = implied_sex(codes)
        if implied is not None:
            candidate_ids, prefilter_dropped = await _narrow_by_sex(
                session, tenant, candidate_ids, implied
            )
            prefilter_sex = implied
            log.info(
                "sourcing_sex_prefilter_applied",
                sourcing_run_id=run_id,
                sex=implied,
                dropped=prefilter_dropped,
            )

        loaded = await load_scoring_inputs(session, tenant_id=tenant, candidate_ids=candidate_ids)

        # --- Semantic retrieval (the recall half of hybrid matching) ---
        # Embed the job order once and find the nearest CVs. The result feeds
        # two paths: the `semantic` score component (precision — a CV aligned
        # with the JD ranks higher) and the rescue path below (recall — a CV
        # that matches the JD even when no structured field did). Empty when
        # embeddings are not configured or no candidate has one yet; both are
        # graceful, and the six-component scorer runs unchanged. The codes
        # passed in are the protected-attribute rows loaded for redaction,
        # reused by `explain_matches` below so they are fetched once per run.
        semantic_scores, _ = await _semantic_scores(
            session, tenant, opportunity, candidate_ids, codes=codes
        )

        today = _today()
        scored: list[tuple[uuid.UUID, object, list]] = []
        rescued: set[uuid.UUID] = set()
        for candidate_id in candidate_ids:
            entry = loaded.get(candidate_id)
            if entry is None:  # pragma: no cover - deleted between the two reads
                continue
            candidate, roles, skills = entry
            total, components = score_candidate(
                opportunity,
                candidate,
                roles,
                skills,
                semantic_scores=semantic_scores,
                today=today,
            )
            if total is None:
                # Nothing about this person was comparable to this job order on
                # the structured fields. The rescue path below may still keep
                # them if their CV is close enough to the JD in meaning — the
                # React-vs-ReactJS case the structured matcher cannot see.
                continue
            scored.append((candidate_id, total, components))

        # --- RRF rescue: candidates the structured scorer dropped but the CV
        # matches. These are people with no title, no skills, no dated roles —
        # nothing the six components could read — whose CV nonetheless lines up
        # with the job order. They were invisible before embeddings; here they
        # earn a floor score from similarity alone, annotated so a recruiter
        # sees why someone with no structured record appears.
        if semantic_scores:
            already_scored = {c for c, _, _ in scored}
            for candidate_id, similarity in semantic_scores.items():
                if candidate_id in already_scored:
                    continue
                if similarity < settings.SOURCING_SEMANTIC_FLOOR:
                    continue
                entry = loaded.get(candidate_id)
                if entry is None:
                    # The neighbour is eligible but was not in the loaded set —
                    # a deleted-between-reads race. Skip rather than crash.
                    continue
                candidate, roles, skills = entry
                weight = _semantic_weight()
                total, components = score_candidate(
                    opportunity,
                    candidate,
                    roles,
                    skills,
                    semantic_scores=semantic_scores,
                    today=today,
                )
                if total is not None:
                    # They had structured data after all and were scored; the
                    # `total is None` branch above was not their path. Keep the
                    # real score, do not synthesise a floor.
                    scored.append((candidate_id, total, components))
                    continue
                # Synthesise the floor: a score from semantic similarity alone,
                # with a note a recruiter reads as "we have no structured
                # record, but the CV matches." The floor weight is the
                # configured semantic weight, so this candidate competes on the
                # same scale as everyone else rather than at an arbitrary
                # number.
                floor = _decimal_floor(similarity * float(weight))
                components = [
                    Component(
                        name="semantic",
                        weight=weight,
                        raw=_decimal_floor(similarity),
                        contribution=floor,
                        note="Matched by CV content; no structured fields on record.",
                    )
                ]
                scored.append((candidate_id, floor, components))
                rescued.add(candidate_id)

        # Descending by score, then by id, so the shortlist a run produces is
        # the same order the stored run reads back in. `explain_matches` sorts
        # again internally and is entitled to; this is about what the run
        # records, not about what the model is shown.
        scored.sort(key=lambda item: (-item[1], str(item[0])))

        # What the run keeps. Everything eligible was scored a moment ago, but
        # a run that stores all of it is a data dump: an agency with two
        # thousand candidates would write, serialise and render two thousand
        # rows off one screen. Cutting here rather than at the reader means
        # the rows that survive are the best ones, because this is the only
        # place the full ranking exists.
        kept = scored[: settings.SOURCING_MAX_MATCHES]
        shortlist = kept[: settings.SOURCING_EXPLAIN_TOP_N]
        texts = await _cv_texts(session, tenant, [candidate_id for candidate_id, _, _ in shortlist])
        # `codes` were loaded once by `_semantic_scores` for redaction; the
        # same rows are what `explain_matches` redacts with, so they are
        # passed through rather than re-fetched.

        explanations, report = await explain_matches(
            opportunity,
            [
                MatchCandidate(
                    candidate_id=candidate_id,
                    full_name=loaded[candidate_id][0].full_name,
                    current_title=loaded[candidate_id][0].current_title,
                    skills=[s.skill_normalized or s.skill for s in loaded[candidate_id][2]],
                    score=total,
                    cv_text=texts.get(candidate_id),
                )
                for candidate_id, total, _ in shortlist
            ],
            codes=codes,
        )
        by_candidate = {str(e.candidate_id): e for e in explanations}

        written = await record_matches(
            session,
            tenant_id=tenant,
            run_id=record,
            matches=[
                {
                    "candidate_id": candidate_id,
                    "score": total,
                    "reasons": serialize_components(components),
                    "explanation": _prose(by_candidate.get(str(candidate_id))),
                    "explanation_evidence": _evidence(by_candidate.get(str(candidate_id))),
                }
                for candidate_id, total, components in kept
            ],
        )

        run = await session.get(SourcingRun, record)
        if run is None:  # pragma: no cover - deleted mid-run
            return
        # What was actually scored is what is counted, even though only `kept`
        # of them were stored: "we scored two thousand and these are the best
        # twenty" is the thing a recruiter wants to know. Where the client's
        # coded sex preference narrowed the pool first, the count is the
        # narrowed roster — `prefilter_dropped` in the run's note says how many
        # the preference removed, so the two figures together read as the full
        # eligible roster without the count overstating who was scored.
        run.candidates_considered = len(candidate_ids)
        run.shortlisted = len(kept)
        run.model_name = settings.EXTRACTION_MODEL_FAST or None
        run.prompt_version = settings.PROMPT_VERSION
        # The protected-attribute report lands here or nowhere. A model told
        # to notice a discriminatory requirement, whose noticing goes into a
        # local variable, is a comment rather than a safeguard — the note is
        # what lets a recruiter go back to the client about the job order.
        run.protected_attribute_noticed = bool(
            report.noticed or report.requirements or report.redacted_codes
        )
        run.protected_attribute_note = _note(report)
        # The sex prefilter is an action taken, not something noticed: it has
        # its own flag so a reviewer reads "the pool was narrowed" off the row
        # rather than inferring it from a sentence. The note alongside the
        # protected-attribute report says plainly what happened and why, so the
        # banner can quote it without the reader having to know the mechanism.
        run.sex_prefilter_applied = prefilter_sex is not None
        run.sex_prefilter_value = prefilter_sex
        if prefilter_sex is not None:
            # allow-hardcode: a recruiter-facing sentence, not configuration.
            prefilter_note = (
                f"Shortlist narrowed to {prefilter_sex} candidates based on the "
                f"client's shorthand in the source email; {prefilter_dropped} "
                f"candidate(s) of another sex were not ranked."
            )
            run.protected_attribute_note = (
                prefilter_note
                if not run.protected_attribute_note
                else f"{run.protected_attribute_note} {prefilter_note}"
            )
        run.state = SourcingRun.DONE
        await session.commit()

    log.info(
        "sourcing_run_completed",
        sourcing_run_id=run_id,
        considered=len(candidate_ids),
        matches=written,
        shortlisted=len(kept),
    )


def _prose(explanation) -> str | None:
    """The sentence to show, or nothing.

    An explanation whose quote could not be found on the candidate's page
    arrives here with an empty reason and a note; storing the note in the
    prose column would present "no parsed CV on file" as the reason to hire
    somebody.
    """
    if explanation is None or not explanation.reason:
        return None
    return explanation.reason


def _evidence(explanation) -> str | None:
    if explanation is None or not explanation.reason:
        return None
    return explanation.evidence


def _note(report) -> str | None:
    """What the run says about protected attributes, in one readable line."""
    parts = []
    if report.requirements:
        # allow-hardcode: a sentence shown to a recruiter, not configuration.
        parts.append(
            "The job order states requirements about protected characteristics, "
            "which were ignored when ranking: " + "; ".join(report.requirements)
        )
    if report.redacted_codes:
        parts.append(
            "Coded requirements removed before the job order was sent to the model: "
            + ", ".join(report.redacted_codes)
        )
    return " ".join(parts) or None


async def _narrow_by_sex(
    session,
    tenant: uuid.UUID,
    candidate_ids: list[uuid.UUID],
    sex: str,
) -> tuple[list[uuid.UUID], int]:
    """Drop candidates whose recorded sex conflicts with `sex`; keep unknowns.

    Honours a client's coded preference (C/F, O/F, ...) in the only way that
    actually reaches the recruiter — by who is *in* the shortlist, not by a
    field on the job order. Done before scoring so no work is spent on people
    who will be excluded, and so the semantic/embedding stages never see them
    either.

    **Missing data is not a disqualification**, mirroring `eligibility.py`'s
    `unknown` outcome: a candidate with no recorded sex is kept for the
    recruiter to check, not silently excluded on the strength of an absence.
    Only a definite mismatch (recorded sex is the *other* value) is removed.

    Returns the narrowed id list and how many were dropped, so the run can
    record both `candidates_considered` (pre-filter, what the system looked at)
    and a note saying how many the preference removed — the narrowing visible by
    arithmetic, not by the reader having to know it happened.
    """
    if not candidate_ids:
        return [], 0
    sex_by_id = await candidate_sexes(session, tenant_id=tenant, candidate_ids=candidate_ids)
    kept: list[uuid.UUID] = []
    dropped = 0
    # Preserve the stable id order `eligible_candidates` returned, so a rerun
    # narrows the same list the same way regardless of row fetch order.
    for candidate_id in candidate_ids:
        recorded = sex_by_id.get(candidate_id)
        if recorded is not None and recorded != sex:
            dropped += 1
            continue
        kept.append(candidate_id)
    return kept, dropped


async def _cv_texts(session, tenant: uuid.UUID, candidate_ids: list[uuid.UUID]) -> dict:
    """The extracted CV text for the shortlist, by candidate.

    A key that no longer resolves to an object is treated as no text at all:
    an explanation with nothing to check against must be refused, and that is
    exactly what `explain_matches` does with a candidate whose `cv_text` is
    `None`.
    """
    keys = await parsed_text_keys(session, tenant_id=tenant, candidate_ids=candidate_ids)
    if not keys:
        return {}
    store = body_store()
    texts = {}
    for candidate_id, key in keys.items():
        text_body = await store.get(key)
        if text_body:
            texts[candidate_id] = text_body
    return texts


async def _fail(tenant: uuid.UUID, record: uuid.UUID) -> None:
    """Park the run in `failed`, terminal so the sweep stops seeing it."""
    async with tenant_session(tenant) as session:
        run = await session.get(SourcingRun, record)
        if run is None:  # pragma: no cover - deleted mid-run
            return
        run.state = SourcingRun.FAILED
        await session.commit()
