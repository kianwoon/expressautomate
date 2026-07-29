"""WhatsApp outreach to one candidate: the draft, and the send.

Separate from `candidates.py` for the reason `candidates_avatar.py` gives —
that file is at the size ceiling — and because this is not really about
candidate *records*. It is about a claim we are entitled to make: whether a
message reached WhatsApp, and what we may write down when we do not know.

Two paths, and they say different things (plan §7-§8):

- `GET /whatsapp-draft` renders the message; the recruiter presses send inside
  WhatsApp Web. This process never sees that send, so the row it eventually
  writes is `whatsapp_opened` / `opened` — through `POST /activities`, which
  stays in `candidates.py` with the rest of the timeline.
- `POST /whatsapp-send` hands the message to the recruiter's own paired
  session through the gateway. This process *does* observe the handover, so
  it may write `sent`, `failed` or `unknown`.

Three rules shape the send path, and each exists because its opposite is a
lie the recruiter would act on:

1. **A row records an attempt, so no attempt means no row.** Refusals that
   happen before dispatch — no paired session, no phone number on file, the
   daily cap already reached — return an error and write nothing.
2. **`failed` requires a refusal we can quote.** If the gateway explicitly
   refused, `error` holds its own words verbatim. If we merely lost the
   connection after dispatching, the answer is `unknown`, because the message
   may well have gone out and telling a recruiter it failed invites them to
   send it twice.
3. **A repeat of an idempotency key is not a second send.** The same
   `client_request_id` returns the original row, unchanged, with 200.
"""

import uuid
from datetime import UTC, datetime, time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.api.auth import _require_session
from app.api.candidates import _load
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateActivity
from app.models.tenant import Tenant, User
from app.services.user_naming import recruiter_name
from app.services.wa_gateway import (
    GatewayOutcomeUnknownError,
    GatewayRefusedError,
    GatewayUnreachableError,
    WaGatewayClient,
)

log = get_logger(__name__)
router = APIRouter(tags=["candidates"])


# Letters whose *name* opens with a vowel sound, for a title that begins with
# an initialism. "HR Manager" is read "aitch-are", so it takes "an", while its
# spelled-out twin "Human Resources Manager" takes "a" — the article follows
# the sound, and for an initialism the sound is the letter's name.
_VOWEL_SOUNDED_LETTERS = frozenset("AEFHILMNORSX")


def _article_for(noun_phrase: str) -> str:
    """"a" or "an", chosen by how the phrase is said rather than spelled.

    The draft used to hardcode "a", which wrote "a Engineer" and — for the
    Enrolled Nurse in the plan this feature was built from — "a Enrolled
    Nurse". It is the first line a candidate reads from the agency, so it is
    worth getting right.

    Two rules, because job titles are mostly ordinary words with a seam of
    initialisms running through them:

    - An initialism — a short all-caps first word (≤4 letters, as in "HR",
      "IT", "QA", "MRT", "NTUC") in a title that is not otherwise shouting —
      is read letter by letter, so the first letter's *name* decides.
    - Anything else goes by its first letter, with `u` deliberately excluded
      from the vowels: a title starting with u almost always says "you" —
      "a UX Designer", "a Unit Manager", "a University Lecturer".

    Both rules are about spelling standing in for pronunciation, so both can
    be wrong: "an Hour" and "a Euro" are the classic misses. Neither shape
    occurs in a job title, and the recruiter edits the message before it is
    sent, so the cost of a miss is a word they retype — not a wrong claim
    about the candidate.
    """
    stripped = noun_phrase.lstrip()
    if not stripped or not stripped[0].isalpha():
        # Nothing to read — "a" is the safer default, and a title starting
        # with a digit ("3D Artist") is said "three-dee" anyway.
        return "a"
    # Two signals have to agree before a word is read as initials.
    #
    # Short: four letters covers HR, IT, QA, UX, MRT and NTUC without
    # reaching an ordinary word. Three would be tidier but loses the
    # four-letter Singapore initialisms, which are the commoner case here.
    #
    # And not shouting: a title arriving in caps lock from CV extraction is
    # every word capitalised, not an initialism, so "HEAD OF SALES" must not
    # become "an HEAD OF SALES". Where the whole title is upper case there is
    # no signal left to separate the two, and reading it as words is wrong
    # less often — it costs "a HR MANAGER" and saves every shouted sentence.
    first_word = stripped.split()[0]
    shouting = stripped.isupper() and " " in stripped.strip()
    if not shouting and first_word.isupper() and first_word.isalpha() and len(first_word) <= 4:
        return "an" if first_word[0] in _VOWEL_SOUNDED_LETTERS else "a"
    return "an" if stripped[0].lower() in "aeio" else "a"


# allow-hardcode: this is the only outreach template step 1 ships, and its
# wording is user-facing copy the recruiter reviews and can edit in the UI
# before opening WhatsApp — the same category `frontend/app/dashboard/
# candidates/page.tsx` marks with this tag for its own hardcoded copy.
def whatsapp_draft_text(
    *,
    candidate_greeting_name: str,
    recruiter_name: str | None,
    agency_name: str,
    job_title: str | None,
) -> str:
    # With a title: "...regarding a Senior Engineer opportunity." Without one,
    # the sentence is rewritten rather than leaving a blank ("regarding a
    # opportunity") or a dangling article ("regarding a  opportunity") — step
    # 1 has no job selector, so a candidate with no `current_title` on file is
    # the common case, not an edge case.
    if job_title:
        article = _article_for(job_title)
        interest_line = (
            f"I would like to speak with you regarding {article} {job_title} opportunity."
        )
    else:
        interest_line = "I would like to speak with you regarding an opportunity."
    # Same treatment as a missing job title: rewrite the sentence rather than
    # leave a gap. `recruiter_name` is None when neither `preferred_name` nor
    # `display_name` is set (see app/services/user_naming.py) — that chain
    # deliberately has no email fallback, since this sentence is read by the
    # candidate, not a colleague.
    if recruiter_name:
        intro_line = f"This is {recruiter_name} from {agency_name}."
    else:
        intro_line = f"I am writing from {agency_name}."
    return (
        f"Hi {candidate_greeting_name},\n\n"
        f"{intro_line}\n\n"
        f"{interest_line}\n\n"
        "Would you be available for a quick discussion?"
    )


@router.get("/candidates/{candidate_id}/whatsapp-draft")
async def whatsapp_draft(request: Request, candidate_id: uuid.UUID) -> dict:
    """The message a recruiter reviews before WhatsApp Web opens (step 1).

    Rendered server-side so the template lives in one testable place instead
    of being duplicated in the frontend. The platform never sends this — see
    `CandidateActivity`.
    """
    user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        if candidate.phone_e164 is None:
            # 409, not 404: the candidate exists, but `_identity_phone` stores
            # NULL here for any number that could not identify a person as a
            # WhatsApp-reachable mobile — including a switchboard/fixed line —
            # so "no phone_e164" means "no number WhatsApp could reach".
            raise HTTPException(
                status_code=409,
                detail=(
                    "This candidate has no mobile number on file that WhatsApp "
                    "could reach. Add a phone number first."
                ),
            )
        user = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one()
        tenant_name = (
            await session.execute(select(Tenant.name).where(Tenant.id == tenant_uuid))
        ).scalar_one()

    # The whole name, because which part of it is the given name is not
    # something this code can know. "Tan Hui Ling" is surname-first, so the
    # first token is `Tan` and greeting her by it addresses a stranger by her
    # surname; written the other way round the first token is `Hui`, half of
    # a two-syllable given name. Malay (bin/binti) and Indian (s/o, d/o)
    # names break a positional rule differently again, and this is a Singapore
    # vertical where all four conventions sit in the same list.
    #
    # So the draft greets the name as recorded and the recruiter shortens it
    # — they know the person. Picking a token here would be the same guess
    # §15 forbids everywhere else, made in the first line the candidate reads.
    candidate_greeting_name = candidate.full_name.strip()
    message = whatsapp_draft_text(
        candidate_greeting_name=candidate_greeting_name,
        recruiter_name=recruiter_name(user.preferred_name, user.display_name),
        agency_name=tenant_name,
        job_title=candidate.current_title,
    )
    return {"phone_e164": candidate.phone_e164, "message": message}


class WhatsAppSendIn(BaseModel):
    """The recruiter's own text, or nothing to use the rendered draft.

    `message` is optional so the send path and the popup path put the *same*
    words on the wire: leaving it out renders `whatsapp_draft_text` exactly as
    `GET /whatsapp-draft` does, rather than a second copy of the template
    living in whichever caller forgot to fetch the draft first.

    `client_request_id` is required, and required rather than optional on
    purpose: what it prevents is a recruiter sending the same message to the
    same candidate twice because the first response was slow, and an optional
    key is the one a nervous caller omits exactly when it matters. The browser
    mints it once per composed message — not per click — so every retry of
    that message carries the same value.
    """

    message: str | None = None
    client_request_id: uuid.UUID


# The status in a 200 body. A plain `str` rather than a closed `Literal`, so
# P5 can add `queued` without this annotation being the thing that has to
# change first. The vocabulary is enforced in `CandidateActivity.STATUSES` and
# in the database CHECK; a second, narrower list here would only drift.
SendStatus = str


async def _draft_for(
    session, candidate: Candidate, user_uuid: uuid.UUID, tenant_uuid: uuid.UUID
) -> str:
    """`whatsapp_draft_text` with the same inputs `GET /whatsapp-draft` gives it."""
    user = (await session.execute(select(User).where(User.id == user_uuid))).scalar_one()
    tenant_name = (
        await session.execute(select(Tenant.name).where(Tenant.id == tenant_uuid))
    ).scalar_one()
    return whatsapp_draft_text(
        candidate_greeting_name=candidate.full_name.strip(),
        recruiter_name=recruiter_name(user.preferred_name, user.display_name),
        agency_name=tenant_name,
        job_title=candidate.current_title,
    )


async def _record_send_attempt(
    *,
    tenant_uuid: uuid.UUID,
    user_uuid: uuid.UUID,
    candidate_id: uuid.UUID,
    client_request_id: uuid.UUID,
    message: str,
    status: str,
    provider_message_id: str | None = None,
    error: str | None = None,
) -> CandidateActivity:
    """Write the outcome of one dispatch, and return the row that now stands.

    Called only when a message was actually handed to the gateway — see rule 1
    in the module docstring.

    Its own transaction, opened after the gateway call has returned: a failed
    attempt must be recorded just as durably as a successful one, and the
    route answers a failure from inside an `except` block, where an insert
    made in a transaction still open around it could be rolled back.

    The `IntegrityError` branch is the race the unique index exists for. Two
    simultaneous clicks can both find no prior row and both dispatch — nothing
    on this side can prevent that — and then both try to insert. The loser
    returns the winner's row, so the recruiter sees one record with one
    outcome rather than a constraint violation.
    """
    activity_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_uuid) as session:
            await session.execute(
                insert(CandidateActivity).values(
                    id=activity_id,
                    tenant_id=tenant_uuid,
                    candidate_id=candidate_id,
                    user_id=user_uuid,
                    client_request_id=client_request_id,
                    activity_type=CandidateActivity.WHATSAPP_SENT,
                    channel=CandidateActivity.WHATSAPP,
                    message_text=message,
                    status=status,
                    provider_message_id=provider_message_id,
                    error=error,
                )
            )
    except IntegrityError:
        existing = await _replay_of(tenant_uuid, client_request_id)
        if existing is None:
            raise
        return existing

    stored = await _replay_of(tenant_uuid, client_request_id)
    if stored is None:  # pragma: no cover — the insert above just committed
        raise HTTPException(status_code=500, detail="the send record could not be read back")
    return stored


async def _replay_of(
    tenant_uuid: uuid.UUID, client_request_id: uuid.UUID
) -> CandidateActivity | None:
    """The row this idempotency key already wrote, if any.

    Read through `tenant_session`, so agency A's key can never surface agency
    B's row even though the two could in principle collide (§18) — which is
    also why the unique index is on `(tenant_id, client_request_id)` rather
    than on the key alone.
    """
    async with tenant_session(tenant_uuid) as session:
        return (
            await session.execute(
                select(CandidateActivity).where(
                    CandidateActivity.client_request_id == client_request_id
                )
            )
        ).scalar_one_or_none()


async def _sent_today(tenant_uuid: uuid.UUID, user_uuid: uuid.UUID) -> int:
    """How many messages this recruiter has actually sent since UTC midnight.

    Only `SENT` rows count. A `FAILED` one never reached WhatsApp, and an
    `UNKNOWN` one is precisely the case we refuse to guess about — counting
    either would spend a recruiter's daily allowance on our uncertainty rather
    than on their sending.

    Per user, not per tenant: WhatsApp bans the *number*, and each recruiter
    pairs their own, so one recruiter's volume is not another's risk.

    UTC midnight rather than the agency's local midnight. This is a safety
    rail, not a billing period, and a Singapore agency's day rolling over at
    08:00 local is a smaller surprise than a per-tenant timezone this codebase
    does not store being guessed at.
    """
    midnight = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    async with tenant_session(tenant_uuid) as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(CandidateActivity)
                .where(
                    CandidateActivity.user_id == user_uuid,
                    CandidateActivity.status == CandidateActivity.SENT,
                    CandidateActivity.created_at >= midnight,
                )
            )
        ).scalar_one()


# The sentence the modal shows for each way a send can be refused before it is
# attempted. Keyed by the session status, so the recruiter is told what is
# actually wrong: "still reconnecting, try in a moment" and "WhatsApp logged
# you out, pair again" are different problems with different next actions, and
# collapsing them into "not connected" leaves someone waiting for a
# reconnection that will never come.
#
# allow-hardcode: user-facing copy, the same category as the WhatsApp draft
# template in `candidates.py`.
_UNSENDABLE_DETAIL = {
    "pairing": (
        "Your WhatsApp is still pairing — scan the code, then try again. "
        "You can open WhatsApp Web instead in the meantime."
    ),
    "reconnecting": (
        "Your WhatsApp connection dropped and is reconnecting. Try again in a "
        "moment, or open WhatsApp Web instead."
    ),
    "disconnected": (
        "Your WhatsApp is not connected. Connect it in Settings, or open "
        "WhatsApp Web instead."
    ),
    "logged_out": (
        "WhatsApp ended the link to this device. Pair again in Settings, or "
        "open WhatsApp Web instead."
    ),
    "gateway_unreachable": (
        "We could not reach the WhatsApp service just now, so nothing was "
        "sent. Open WhatsApp Web instead, or try again shortly."
    ),
}

# allow-hardcode: user-facing copy, as above. `{limit}` is filled from
# `settings.WA_SEND_DAILY_LIMIT` — the number itself is configuration.
_DAILY_LIMIT_DETAIL = (
    "You have sent your daily limit of {limit} WhatsApp messages from your "
    "own number. The limit resets at midnight UTC. You can open WhatsApp Web "
    "instead if this one cannot wait."
)


def _unsendable(status: str) -> JSONResponse:
    """409, shaped so the modal can fall back to the popup path.

    A `JSONResponse`, not an `HTTPException`, and therefore *returned* rather
    than raised. `HTTPException(detail={...})` nests whatever dict it is given
    under FastAPI's own `detail` key, producing `{"detail": {"detail": ...}}`;
    the contract — and the frontend's shared `readError`, which reads
    `body.detail` as a plain string for every endpoint — requires `detail` and
    `session_status` to be siblings at the top level.

    Returned only *before* dispatch, so no activity row accompanies it.
    """
    return JSONResponse(
        status_code=409,
        content={
            "detail": _UNSENDABLE_DETAIL.get(status, _UNSENDABLE_DETAIL["disconnected"]),
            "session_status": status,
        },
    )


def _send_response(activity: CandidateActivity) -> dict:
    """The 200 body, built from the row rather than from local variables.

    Which matters for the replay case: a repeated `client_request_id` returns
    the *original* row's outcome, so the second caller is told what actually
    happened the first time and not what this request would have done.
    """
    return {
        "status": activity.status,
        "activity_id": str(activity.id),
        "provider_message_id": activity.provider_message_id,
        "client_request_id": str(activity.client_request_id),
        # Only ever set on a `failed` row, and then it is the gateway's own
        # words. Present in the body so the modal can show the recruiter the
        # real reason instead of a generic failure.
        "error": activity.error,
    }


@router.post("/candidates/{candidate_id}/whatsapp-send", response_model=None)
async def whatsapp_send(
    request: Request, candidate_id: uuid.UUID, body: WhatsAppSendIn
) -> dict | JSONResponse:
    """Send through the recruiter's own paired WhatsApp (plan §7).

    §15, and the reason `status='sent'` is writable at all: `sent` means the
    gateway's socket accepted the message and WhatsApp returned a message id.
    It does not mean delivered and never means read — no receipts are ingested
    in v1. `unknown` means we dispatched and never learned the outcome. See
    the module docstring for the three rules this route enforces.

    Never 500s and never leaks. A dead gateway, a timeout and a 5xx are
    answered with a body the modal can act on, because plan §7's whole design
    is that a broken send degrades to the popup path that already works; and
    nothing in a response or in `candidate_activities.error` carries the
    gateway URL or the shared secret.

    One candidate, one message. There is deliberately no bulk form (plan §9).
    """
    user_uuid, tenant_uuid = _require_session(request)

    # Before anything else, including the candidate lookup: a replay must be
    # answered identically no matter what has changed since — the candidate's
    # phone being edited, the session having dropped, the cap having been
    # reached. The original row is the answer, and it is already written.
    replay = await _replay_of(tenant_uuid, body.client_request_id)
    if replay is not None:
        return _send_response(replay)

    async with tenant_session(tenant_uuid) as session:
        # Inside the tenant session, so agency B asking about agency A's
        # candidate gets a 404 before anything else happens (§18).
        candidate = await _load(session, candidate_id)
        message = body.message or await _draft_for(session, candidate, user_uuid, tenant_uuid)
        phone_e164 = candidate.phone_e164

    if phone_e164 is None:
        # 422 and no `session_status`. This is a problem with the candidate's
        # data, not with the recruiter's WhatsApp — the session may be
        # perfectly healthy — and naming a session state here would send
        # someone to Settings to fix something that is not broken. Nothing was
        # attempted, so nothing is recorded.
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "This candidate has no mobile number on file that WhatsApp "
                    "could reach. Add a phone number first."
                )
            },
        )

    sent_today = await _sent_today(tenant_uuid, user_uuid)
    if sent_today >= settings.WA_SEND_DAILY_LIMIT:
        # 429, with the same body shape as the 409s above so the modal's
        # existing fallback-to-popup branch handles it without a second code
        # path. `session_status` is `gateway_unreachable`-adjacent nonsense
        # here, so it is absent: the session is fine, the allowance is not.
        # Nothing was attempted, so nothing is recorded.
        log.info(
            "wa_send_daily_limit_reached",
            sent_today=sent_today,
            limit=settings.WA_SEND_DAILY_LIMIT,
        )
        return JSONResponse(
            status_code=429,
            content={"detail": _DAILY_LIMIT_DETAIL.format(limit=settings.WA_SEND_DAILY_LIMIT)},
        )

    if not settings.wa_gateway_configured():
        # A `gateway` service that was never given its env vars is a real
        # deployment state, not an exception — CLAUDE.md's GRAPH_BASE_URL and
        # R2_* precedent. Answered as unreachable, which is what it is, and
        # nothing was dispatched so nothing is recorded.
        return _unsendable("gateway_unreachable")

    try:
        outcome = await WaGatewayClient().send(
            str(tenant_uuid), str(user_uuid), to=phone_e164, text=message
        )
    except GatewayUnreachableError:
        # The connection never came up: the request did not go out.
        return _unsendable("gateway_unreachable")
    except GatewayRefusedError as exc:
        # A real, observed refusal on a live socket. This is the one path that
        # writes `failed`, and `exc.message` is the gateway's own wording,
        # stored verbatim.
        activity = await _record_send_attempt(
            tenant_uuid=tenant_uuid,
            user_uuid=user_uuid,
            candidate_id=candidate_id,
            client_request_id=body.client_request_id,
            message=message,
            status=CandidateActivity.FAILED,
            error=exc.message,
        )
        return JSONResponse(
            status_code=409,
            content={
                "detail": exc.message,
                # The session itself was up; it was this message WhatsApp
                # would not take. Named so the modal does not send the
                # recruiter to Settings to re-pair a working connection.
                "session_status": "connected",
                "activity_id": str(activity.id),
            },
        )
    except GatewayOutcomeUnknownError:
        # Dispatched, outcome never learned. Recorded as `unknown` and
        # answered as `unknown` — a 200, because something did happen and the
        # recruiter needs the record, not an error that reads as "try again".
        activity = await _record_send_attempt(
            tenant_uuid=tenant_uuid,
            user_uuid=user_uuid,
            candidate_id=candidate_id,
            client_request_id=body.client_request_id,
            message=message,
            status=CandidateActivity.UNKNOWN,
            # Our own sentence, and allowed to be, because `unknown` is our
            # own conclusion rather than a quotation. `error` is only ever the
            # gateway's verbatim words on a `failed` row.
            error=(
                "The message was sent to WhatsApp but no confirmation came "
                "back, so we do not know whether it went out."
            ),
        )
        return _send_response(activity)
    except Exception as exc:  # noqa: BLE001 — see below
        # Anything else the client can raise (an unexpected error whose
        # *string carries the URL*, e.g. `httpx.HTTPStatusError`) is caught
        # and replaced, never re-raised and never formatted into a response or
        # a column. We cannot tell whether this dispatched, so it is `unknown`
        # by the same rule as above, not `failed`.
        log.warning("wa_gateway_send_failed", error=type(exc).__name__)
        activity = await _record_send_attempt(
            tenant_uuid=tenant_uuid,
            user_uuid=user_uuid,
            candidate_id=candidate_id,
            client_request_id=body.client_request_id,
            message=message,
            status=CandidateActivity.UNKNOWN,
            error=(
                "The message was sent to WhatsApp but no confirmation came "
                "back, so we do not know whether it went out."
            ),
        )
        return _send_response(activity)

    if not outcome.ok:
        # The gateway declined before touching WhatsApp: the session is not
        # connected. Nothing was attempted, so nothing is recorded.
        return _unsendable(outcome.session_status)

    activity = await _record_send_attempt(
        tenant_uuid=tenant_uuid,
        user_uuid=user_uuid,
        candidate_id=candidate_id,
        client_request_id=body.client_request_id,
        message=message,
        status=CandidateActivity.SENT,
        provider_message_id=outcome.provider_message_id,
    )
    return _send_response(activity)
