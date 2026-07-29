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

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.opportunity import Opportunity
from app.models.opportunity_code import OpportunityCode
from app.models.sourcing import SourcingRun
from app.services.sourcing.eligible import eligible_candidates
from app.services.sourcing.explain import MatchCandidate, explain_matches
from app.services.sourcing.persist import (
    load_scoring_inputs,
    parsed_text_keys,
    record_matches,
    serialize_components,
)
from app.services.sourcing.score import score_candidate
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
        log.warning(
            "sourcing_attempts_exhausted", sourcing_run_id=run_id, attempts=attempts
        )
        await _fail(tenant, record)
        return

    async with tenant_session(tenant) as session:
        opportunity = (
            await session.execute(
                select(Opportunity).where(Opportunity.id == opportunity_key)
            )
        ).scalar_one_or_none()
        if opportunity is None:
            # The job order was deleted between the request and the worker.
            # There is nothing left to rank against, and no useful run to
            # store — `failed` is what the panel should show.
            log.info("sourcing_opportunity_missing", sourcing_run_id=run_id)
            await _fail(tenant, record)
            return

        candidate_ids = await eligible_candidates(
            session, tenant_id=tenant, client_id=client
        )
        loaded = await load_scoring_inputs(
            session, tenant_id=tenant, candidate_ids=candidate_ids
        )

        today = _today()
        scored: list[tuple[uuid.UUID, object, list]] = []
        for candidate_id in candidate_ids:
            entry = loaded.get(candidate_id)
            if entry is None:  # pragma: no cover - deleted between the two reads
                continue
            candidate, roles, skills = entry
            total, components = score_candidate(
                opportunity, candidate, roles, skills, today=today
            )
            if total is None:
                # Nothing about this person was comparable to this job order.
                # Dropped rather than stored, both because the column is NOT
                # NULL and because a zero here would libel them.
                continue
            scored.append((candidate_id, total, components))

        # Descending by score, then by id, so the shortlist a run produces is
        # the same order the stored run reads back in. `explain_matches` sorts
        # again internally and is entitled to; this is about what the run
        # records, not about what the model is shown.
        scored.sort(key=lambda item: (-item[1], str(item[0])))

        shortlist = scored[: settings.SOURCING_EXPLAIN_TOP_N]
        texts = await _cv_texts(
            session, tenant, [candidate_id for candidate_id, _, _ in shortlist]
        )
        codes = list(
            (
                await session.execute(
                    select(OpportunityCode).where(
                        OpportunityCode.opportunity_id == opportunity_key
                    )
                )
            ).scalars()
        )

        explanations, report = await explain_matches(
            opportunity,
            [
                MatchCandidate(
                    candidate_id=candidate_id,
                    full_name=loaded[candidate_id][0].full_name,
                    current_title=loaded[candidate_id][0].current_title,
                    skills=[
                        s.skill_normalized or s.skill for s in loaded[candidate_id][2]
                    ],
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
                for candidate_id, total, components in scored
            ],
        )

        run = await session.get(SourcingRun, record)
        if run is None:  # pragma: no cover - deleted mid-run
            return
        run.candidates_considered = len(candidate_ids)
        run.shortlisted = len(shortlist)
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
        run.state = SourcingRun.DONE
        await session.commit()

    log.info(
        "sourcing_run_completed",
        sourcing_run_id=run_id,
        considered=len(candidate_ids),
        matches=written,
        shortlisted=len(shortlist),
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
