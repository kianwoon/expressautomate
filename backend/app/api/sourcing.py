"""Start a shortlist, read one back, and record who was actually submitted.

Its own module rather than more of `opportunities.py` or `candidates.py`,
because these routes straddle both and belong to neither: a run is started
against a job order and read back against a job order, but a submission is
recorded against a candidate, and the two only make sense together. Without
the submission routes the eligibility rule "not already submitted to this
client" can never fire, because nothing would ever write the row it reads.

Three things are decided here rather than in the worker, and each of them is
here for a reason that only holds on this side of the queue:

1. **The client.** There is no `opportunities.client_id`; the link is the
   email the job order arrived on. Resolving it here means the answer is
   written onto the run (`client_resolution.py` explains the rule), so the run
   records which client it excluded against — or says plainly that it could
   not tell, and that the exclusion therefore did not run. The worker used to
   infer it on every attempt and substitute a nil UUID on failure, which
   disabled the exclusion silently: a candidate already put in front of that
   client would reappear at the top of the next shortlist with nothing
   anywhere to say why.

2. **The quota.** `SOURCING_DAILY_RUN_QUOTA` is checked before the row is
   created. Refusing inside the worker would leave `pending` rows nobody will
   ever process — a queue of runs that look started and never finish.

3. **The failure to queue.** `enqueue` returns a bool and never raises, so a
   Redis outage would otherwise leave a run `pending` until the stuck-run
   sweep happened by. The run is moved to `failed` with a sentence saying a
   retry is worth trying, exactly as `candidate_imports.py` does.

Another agency's job order, run, candidate or submission is a **404, never a
403**: every read goes through the tenant session, so a foreign id is simply
not there.

The tenant session is no longer the whole answer for a job order. RLS draws
the line between agencies; inside one agency a job order belongs to a
recruiter, and `app/services/visibility.py` owns that rule. Every route here
that names an `opportunity_id` therefore loads it through
`load_visible_opportunity` — reading it under RLS alone would hand a colleague
another recruiter's shortlist, names and scores, and let them spend that
recruiter's daily run quota. Still a 404, for the same reason as above: a 403
would confirm the row exists.
"""

import uuid
from datetime import UTC, datetime, time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import Candidate
from app.models.client import Client
from app.models.sourcing import CandidateSubmission, SourcingRun
from app.services.candidate_matching import masked_candidate
from app.services.sourcing.client_resolution import resolve_client
from app.services.sourcing.persist import read_matches
from app.services.visibility import (
    can_edit_candidate,
    load_visible_candidate,
    load_visible_opportunity,
    opportunity_chain_ids,
    visible_candidates,
)
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(tags=["sourcing"])

# allow-hardcode: a sentence shown to a recruiter, not configuration.
_ENQUEUE_FAILED = (
    "This shortlist was created but could not be queued. Try again in a few minutes."
)


def serialize_run(run: SourcingRun) -> dict:
    """What the panel needs to describe one run.

    `client_id` and `client_unresolved_reason` are both exposed, and the UI
    needs both: the id says which client the already-submitted exclusion was
    applied against, and the reason is the only thing that distinguishes "no
    candidate had been submitted to them" from "we never checked".
    """
    return {
        "id": str(run.id),
        "opportunity_id": str(run.opportunity_id),
        "state": run.state,
        "client_id": str(run.client_id) if run.client_id else None,
        "client_unresolved_reason": run.client_unresolved_reason,
        "candidates_considered": run.candidates_considered,
        "shortlisted": run.shortlisted,
        "protected_attribute_noticed": run.protected_attribute_noticed,
        "protected_attribute_note": run.protected_attribute_note,
        "sex_prefilter_applied": run.sex_prefilter_applied,
        "sex_prefilter_value": run.sex_prefilter_value,
        "failure_reason": run.failure_reason,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def serialize_match(
    match, *, visible: bool, masked: dict | None = None, submitted: bool = False
) -> dict:
    """One match, disclosed to exactly the tier this viewer is entitled to.

    Sourcing scores the whole agency on purpose — an agency that cannot
    shortlist across its own book has no reason to run sourcing at all — so a
    shortlist routinely names people the viewer may not see. Filtering them
    out would gut the product; returning them in full would make the 409
    collision path's masking theatre. So the match stays and the *content*
    goes, down to the tier `held_by_colleague` already defines for the 409:
    an abbreviated, contact-masked name, who holds the person, the id, and a
    way to ask. `explanation`, `explanation_evidence` (verbatim CV quotes)
    and `reasons` are withheld entirely rather than trimmed — each is free
    text about a person this viewer has no claim on.

    **The score stays, and that was considered.** It reveals how well the
    person fits this job order, not anything about them: no name beyond the
    abbreviation, no history, no quote. Withholding it would leave a row that
    cannot be ranked or reasoned about, which is the same as dropping it.

    `submitted` is whether the candidate already stands submitted to the
    run's resolved client — computed at read time, so a submission recorded
    after the run was scored (by this recruiter or a colleague) shows up on
    the next read rather than being frozen into the stored run. The flag is
    still carried on a redacted match: it is a fact about the candidate's
    relationship to the client, the same tier as the id itself, and the
    redacted row renders no submit affordance to act on it.
    """
    common = {
        "candidate_id": str(match.candidate_id),
        # A string, not a float: the column is NUMERIC(6, 4) and binary
        # floating point cannot hold every value it stores exactly. The four
        # places are the whole reason the column was widened, so rounding them
        # away on the way out would undo that in the last step.
        "score": str(match.score),
        "visible": visible,
        "submitted": submitted,
    }
    if visible:
        return {
            **common,
            "reasons": match.reasons,
            "explanation": match.explanation,
            "explanation_evidence": match.explanation_evidence,
        }
    return {
        **common,
        # `masked` is None only if the row vanished between the two reads.
        # Say nothing about it rather than inventing a name — and do not offer
        # to ask for access to a record that is no longer there, which would
        # send the recruiter to a button that can only ever 404.
        "full_name": masked["full_name"] if masked else None,
        "held_by": masked["held_by"] if masked else None,
        "can_request_access": masked is not None,
    }


class SubmissionRequest(BaseModel):
    client_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None


def _midnight_utc() -> datetime:
    """The start of the quota window.

    UTC, which is 8am in Singapore — the same window `CV_DAILY_PARSE_QUOTA`
    uses, and deliberately the same rather than a second definition of "today"
    for a recruiter to reconcile.
    """
    return datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)


@router.post("/opportunities/{opportunity_id}/sourcing", status_code=202)
async def start_sourcing(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Queue a shortlist for this job order.

    202, not 201: the row exists but the answer does not. Nothing is scored
    here — an agency's whole candidate database plus a model call has no
    business inside a request.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Resolves supersede chains: if a later email revised this job order's
        # requirements, the run must be recorded against — and later score
        # against — the *current* revision, never the row the client replaced.
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        opportunity_id = current.id

        # Counted, and refused, before anything is written. A run created and
        # then rejected would be a `pending` row no worker will ever claim.
        #
        # Serialised per tenant with a transaction-scoped advisory lock. The
        # count and the insert below are two statements, and two recruiters
        # clicking at once must not both read the count before either has
        # written — without the lock the daily cap is a soft overage, and with
        # it the second caller sees the first's committed run and gets the
        # honest 429. The lock is held until this transaction commits, so the
        # count it protects is the count the insert lands against.
        #
        # `failed` runs do not count. A run whose enqueue failed is terminal —
        # `rescan_stuck` never retries it — so a retry after an outage would
        # otherwise spend a slot on a run that never ran, and a long Redis
        # outage could exhaust the whole daily quota on dead rows.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"tenant:{tenant_uuid}"},
        )
        used = (
            await session.execute(
                select(func.count())
                .select_from(SourcingRun)
                .where(
                    SourcingRun.created_at >= _midnight_utc(),
                    SourcingRun.state != SourcingRun.FAILED,
                )
            )
        ).scalar_one()
        if used >= settings.SOURCING_DAILY_RUN_QUOTA:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"This agency has started {settings.SOURCING_DAILY_RUN_QUOTA} "
                    "shortlists today. More can be started tomorrow."
                ),
            )

        resolution = await resolve_client(
            session, tenant_id=tenant_uuid, opportunity_id=opportunity_id
        )
        run_id = uuid.uuid4()
        session.add(
            SourcingRun(
                id=run_id,
                tenant_id=tenant_uuid,
                opportunity_id=opportunity_id,
                state=SourcingRun.PENDING,
                client_id=resolution.client_id,
                client_unresolved_reason=resolution.reason,
                created_by=user_uuid,
            )
        )
        await session.commit()

    if resolution.client_id is None:
        # Logged as well as stored, because "the exclusion did not run" is the
        # kind of thing that is only noticed later, from the outside.
        log.info(
            "sourcing_client_unresolved",
            sourcing_run_id=str(run_id),
            opportunity_id=str(opportunity_id),
        )

    # Enqueued after the commit, because the job reads the row it is named
    # for. The client goes with it rather than being read back off the row:
    # the queue hop should not depend on this write being visible first.
    if not await enqueue(
        "run_sourcing",
        tenant_id=str(tenant_uuid),
        opportunity_id=str(opportunity_id),
        run_id=str(run_id),
        client_id=str(resolution.client_id) if resolution.client_id else None,
    ):
        log.warning("sourcing_enqueue_failed", sourcing_run_id=str(run_id))
        async with tenant_session(tenant_uuid) as session:
            record = await session.get(SourcingRun, run_id)
            record.state = SourcingRun.FAILED
            record.failure_reason = _ENQUEUE_FAILED
            body = serialize_run(record)
            await session.commit()
            return body

    async with tenant_session(tenant_uuid) as session:
        return serialize_run(await session.get(SourcingRun, run_id))


@router.get("/opportunities/{opportunity_id}/sourcing")
async def latest_sourcing(request: Request, opportunity_id: uuid.UUID) -> dict:
    """The most recent run for this job order, and its matches.

    A job order with no run yet answers 200 with a null run rather than 404:
    "there is no shortlist" is a state of a job order that exists, and a 404
    here would be indistinguishable from another agency's id.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Resolves supersede chains: the runs live against the *current*
        # revision, so a stale id reads the live job order's shortlists. The
        # run may also predate the current revision — a shortlist started
        # against the revision the client replaced is still that job order's
        # history, so the query covers every id in the chain.
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        chain = await opportunity_chain_ids(session, current.id)
        run = (
            await session.execute(
                select(SourcingRun)
                .where(SourcingRun.opportunity_id.in_(chain))
                # `id` breaks the tie: two runs started in the same
                # transaction share `created_at`, and "the latest" must not
                # depend on which one the plan returns first.
                .order_by(SourcingRun.created_at.desc(), SourcingRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            return {"run": None, "matches": []}
        return await _with_matches(session, tenant_uuid, run, user_uuid, role)


@router.get("/opportunities/{opportunity_id}/sourcing/runs")
async def list_sourcing_runs(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Every run for this job order, newest first — the index the panel's run
    history reads.

    Declared before `sourcing/{run_id}` on purpose: a literal segment must
    precede a `{param}` segment on the same prefix, or FastAPI matches
    `/sourcing/runs` to the parameter route first and answers 422 for a UUID
    that is not one — the include-order failure `test_the_sourcing_paths_are_
    declared_and_under_api` exists to catch.

    Runs are never deleted, and a job order gets a handful a day at most, so
    the whole list is cheap to return and the frontend renders it directly.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        chain = await opportunity_chain_ids(session, current.id)
        runs = (
            await session.execute(
                select(SourcingRun)
                .where(SourcingRun.opportunity_id.in_(chain))
                # `id` breaks the tie, exactly as `latest_sourcing` does: two
                # runs created in one transaction share `created_at`, and the
                # order must not depend on which one the plan returns first.
                .order_by(SourcingRun.created_at.desc(), SourcingRun.id.desc())
            )
        ).scalars()
        return {"runs": [serialize_run(run) for run in runs]}


@router.get("/opportunities/{opportunity_id}/sourcing/{run_id}")
async def one_sourcing_run(
    request: Request, opportunity_id: uuid.UUID, run_id: uuid.UUID
) -> dict:
    """An earlier run, so "the list I sent on Tuesday" survives.

    A run is a record rather than a live query — that is why `sourcing_runs`
    stores its matches instead of recomputing them — and a record nobody can
    address by id is not much of one.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        chain = await opportunity_chain_ids(session, current.id)
        run = (
            await session.execute(
                select(SourcingRun).where(
                    SourcingRun.id == run_id,
                    # A real run under the wrong job order is a 404 too: the
                    # URL asserts a relationship, and answering anyway would
                    # let the path be walked for run ids. Runs recorded against
                    # any revision in the chain belong to this job order.
                    SourcingRun.opportunity_id.in_(chain),
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Shortlist not found")
        return await _with_matches(session, tenant_uuid, run, user_uuid, role)


async def _with_matches(
    session, tenant_uuid: uuid.UUID, run: SourcingRun, user_id: uuid.UUID, role: str
) -> dict:
    """One run and its matches, best first and stable — `read_matches` orders
    by score descending then `candidate_id`, so two readers of the same stored
    run see the same list even where scores tie.

    Redaction happens HERE, at read, and not in `persist.py` or `eligible.py`.
    A run is scored once and stored once; who may see what changes afterwards,
    every time a candidate is shared or claimed. Baking one viewer's
    entitlement into the stored run would freeze it at the moment of scoring
    and would have to be recomputed for the next reader anyway.

    One query decides the whole page: `visible_candidates` over the ids this
    run actually names. The per-candidate `masked_candidate` calls that follow
    run only for the ones being redacted, and a shortlist is tens of rows, not
    thousands.
    """
    matches = await read_matches(session, tenant_id=tenant_uuid, run_id=run.id)
    ids = [m.candidate_id for m in matches]
    visible_ids: set[uuid.UUID] = set()
    if ids:
        visible_ids = set(
            (
                await session.execute(
                    select(Candidate.id)
                    .where(Candidate.id.in_(ids))
                    .where(visible_candidates(user_id, role))
                )
            )
            .scalars()
            .all()
        )

    # Which of the run's candidates already stand submitted to the run's
    # resolved client. Read here, at read time, not baked into the stored run:
    # a submission recorded after scoring (a colleague's, or this recruiter's
    # own from an earlier run) must appear on the next read. A run with no
    # resolved client has nothing to be submitted to, so every match is false
    # and the UI disables the action rather than pretending otherwise.
    submitted_ids: set[uuid.UUID] = set()
    if ids and run.client_id is not None:
        submitted_ids = set(
            (
                await session.execute(
                    select(CandidateSubmission.candidate_id).where(
                        CandidateSubmission.tenant_id == tenant_uuid,
                        CandidateSubmission.client_id == run.client_id,
                        CandidateSubmission.candidate_id.in_(ids),
                    )
                )
            )
            .scalars()
            .all()
        )

    serialized = []
    for match in matches:
        visible = match.candidate_id in visible_ids
        masked = (
            None if visible else await masked_candidate(session, match.candidate_id)
        )
        serialized.append(
            serialize_match(
                match,
                visible=visible,
                masked=masked,
                submitted=match.candidate_id in submitted_ids,
            )
        )
    return {"run": serialize_run(run), "matches": serialized}


@router.post("/candidates/{candidate_id}/submissions", status_code=201)
async def record_submission(
    request: Request, candidate_id: uuid.UUID, body: SubmissionRequest
) -> dict:
    """Record that this person was put in front of this client.

    The one durable fact the shortlist exists to produce, and the only thing
    that makes the eligibility exclusion mean anything.

    The client and the job order are looked up rather than trusted: both are
    read through the tenant session first, so an id belonging to another
    agency is a 404 here rather than a foreign key violation later. A repeat
    is 409, not a second row — `uq_candidate_submissions_once_per_client` says
    a person is either in front of a client or not, and a double-click must
    not turn that into a workflow.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)

        client = (
            await session.execute(select(Client).where(Client.id == body.client_id))
        ).scalar_one_or_none()
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")

        if client.status == Client.SUSPENDED:
            # A hold is a commercial decision, and putting a candidate in front
            # of the client is the act it exists to stop. Sourcing and ranking
            # for the same client stay open — see the design note in
            # docs/superpowers/specs/2026-07-30-clients-administration-design.md.
            #
            # The reason is echoed rather than summarised: "this client is
            # suspended" sends the recruiter hunting for why, and the why is
            # already stored.
            detail = f"{client.name} is suspended"
            if client.suspended_reason:
                detail = f"{detail}: {client.suspended_reason}"
            raise HTTPException(status_code=409, detail=detail)

        if body.opportunity_id is not None:
            # Optional, and still guarded: naming a job order you cannot see
            # is how you find out it exists.
            await load_visible_opportunity(
                session, body.opportunity_id, user_uuid, role
            )

        existing = (
            await session.execute(
                select(CandidateSubmission).where(
                    CandidateSubmission.candidate_id == candidate_id,
                    CandidateSubmission.client_id == body.client_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="This candidate has already been submitted to this client.",
            )

        record = CandidateSubmission(
            id=uuid.uuid4(),
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            client_id=body.client_id,
            opportunity_id=body.opportunity_id,
            submitted_by=user_uuid,
        )
        session.add(record)
        try:
            await session.flush()
            # `submitted_at` is a server default, so it is unset on the object
            # until it is read back. Refreshed explicitly rather than left to lazy
            # load: an async session cannot fetch an expired attribute on
            # attribute access, and the response would be a MissingGreenlet.
            await session.refresh(record)
            body_out = _serialize_submission(record)
            await session.commit()
        except IntegrityError:
            # The pre-check above catches the ordinary repeat, but two
            # submissions racing between that read and this write both pass it.
            # The unique key decides, and the loser must read as the same 409
            # the winner's pre-check would have produced — never a 500.
            raise HTTPException(
                status_code=409,
                detail="This candidate has already been submitted to this client.",
            ) from None
        return body_out


@router.delete("/candidates/{candidate_id}/submissions/{submission_id}", status_code=200)
async def withdraw_submission(
    request: Request, candidate_id: uuid.UUID, submission_id: uuid.UUID
) -> dict:
    """Undo a submission recorded in error, restoring the candidate's
    eligibility for that client.

    Deleted rather than flagged: this table answers one boolean question and
    carries no status column on purpose, so a withdrawn submission that stayed
    as a row would keep excluding the candidate while claiming not to.

    Edit rights, not merely visibility — unlike `record_submission` above.
    Recording is additive and follows `start_sourcing`'s precedent: a share
    recipient may shortlist a candidate shown to them, because that is
    visibility, not edit rights. Withdrawing is destructive to whatever a
    colleague recorded, so it needs ownership — with one exception: whoever
    created the submission may undo it even without edit rights on the
    candidate. Otherwise a share recipient could record a submission
    (`record_submission` allows it) but never take it back, so a misclick
    would be permanent for them. Everyone else still needs edit rights.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)
        record = (
            await session.execute(
                select(CandidateSubmission).where(
                    CandidateSubmission.id == submission_id,
                    CandidateSubmission.candidate_id == candidate_id,
                )
            )
        ).scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=404, detail="Submission not found")

        allowed = can_edit_candidate(candidate, user_uuid, role) or (
            record.submitted_by == user_uuid
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="This candidate is shared with you, not assigned to you.",
            )

        body = _serialize_submission(record)
        await session.delete(record)
        await session.commit()
        return {"deleted": True, "submission": body}


def _serialize_submission(record: CandidateSubmission) -> dict:
    return {
        "id": str(record.id),
        "candidate_id": str(record.candidate_id),
        "client_id": str(record.client_id),
        "opportunity_id": str(record.opportunity_id) if record.opportunity_id else None,
        "submitted_at": record.submitted_at.isoformat() if record.submitted_at else None,
    }
