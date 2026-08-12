"""Proving an address belongs to the person who asked for it.

A typed identifier that is one digit wrong delivers a client's job orders to a
stranger, so nothing is ever verified by assertion. Telegram proves it by the
fact that only the account holder could have pressed start on a link; WhatsApp
by a code that only reaches the number typed.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt, encrypt
from app.core.logging import get_logger
from app.models.notification import (
    CHANNEL_WHATSAPP_LINKED,
    address_digest,
)
from app.workers.queue import redis_pool

log = get_logger(__name__)

# The token is compared by hash, so it must hash the same way every time —
# `address_digest` is the same construction and the same reasoning: a stable,
# non-reversible, unsalted digest so equal inputs always collide (which is
# exactly what a lookup needs) and unequal ones don't. It is safe to reuse
# here because token collisions are governed by the token's own entropy (see
# `issue_token`), not by anything address-shaped.
_hash_token = address_digest

# allow-hardcode: SQL statements, not a phrase list.
_INSERT_TOKEN = text(
    """
    INSERT INTO notification_link_tokens
        (id, tenant_id, user_id, channel, token_hash, address_encrypted, expires_at)
    VALUES (:id, :tenant_id, :user_id, :channel, :token_hash, :address_encrypted, :expires_at)
    """
)

# Consumes in the statement that reads, so a token raced by two requests
# resolves for exactly one of them: the UPDATE's row lock makes the second
# concurrent attempt block until the first commits, then its own
# `consumed_at IS NULL` predicate is already false and it matches zero rows.
_CONSUME_TOKEN = text(
    """
    UPDATE notification_link_tokens
    SET consumed_at = now()
    WHERE token_hash = :token_hash
      AND channel = :channel
      AND consumed_at IS NULL
      AND expires_at > now()
    RETURNING tenant_id, user_id, address_encrypted
    """
)

# `user_id` is in the DO UPDATE set on purpose: ownership follows the most
# recent proof, not the first claimant. A phone number or Telegram chat is
# one physical device, so a second user verifying the same address means the
# device changed hands and the person holding it now is the one who just
# proved it — leaving the row attributed to whoever verified it first would
# silently keep sending a colleague's job orders to someone else's phone.
# This is deliberately symmetric across the nullable boundary too: verifying
# with a specific `user_id` claims a previously tenant-shared address as
# personal, and verifying with `user_id=NULL` (the agency re-registering its
# shared feed) surrenders a previously personal claim back to the tenant.
# Both are "most recent proof wins" — the same rule, just written as an
# unconditional assignment rather than a conditional one, since that is what
# keeps it a single obvious rule instead of two special cases to maintain.
_UPSERT_DESTINATION = text(
    """
    INSERT INTO notification_destinations
        (id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at)
    VALUES (:id, :tenant_id, :user_id, :channel, :address_encrypted, :address_hash, now())
    ON CONFLICT (tenant_id, channel, address_hash) DO UPDATE
      SET user_id = EXCLUDED.user_id, verified_at = now(), disabled_at = NULL, failure_count = 0
    RETURNING id
    """
)

_OPT_IN_ATTEMPTS = text(
    """
    SELECT count(*) FROM notification_link_tokens
    WHERE user_id = :user_id
      AND channel = :channel
      AND created_at > now() - interval '1 hour'
    """
)


# Verify attempts, not opt-in requests: opt_in_attempts_this_hour above counts
# rows in notification_link_tokens because *issuing* a code is a write we
# already make. *Guessing* one is not — redeem_token's UPDATE matches zero
# rows on a wrong guess, so there is nothing to count in the database without
# adding a table or a column purely to log failed guesses.
#
# This used to be an in-process dict, on the reasoning that a single-process
# deployment made cross-process visibility "theoretical, not absent". That
# reasoning stopped holding the moment this service could run more than one
# API instance on Koyeb: N instances each keep their own counter, so an
# attacker gets roughly N times the configured budget, and every deploy,
# restart, or scale event zeroes every counter and hands them a fresh one
# mid-attack. Against a six-digit code with a fifteen-minute TTL that is not
# a meaningful bound, so the counter now lives in Redis — already a
# dependency of this process via `app.workers.queue` — keyed per user, with
# the key itself expiring so there is nothing to sweep.
_VERIFY_ATTEMPT_KEY = "notify:verify_attempts:{user_id}"


async def record_verify_attempt(user_id: uuid.UUID) -> int:
    """Record one attempt to redeem a WhatsApp verification code and return
    the running count for this user's current window.

    Fails CLOSED, deliberately unlike `app.workers.queue.enqueue`'s fail-soft
    default. `enqueue` can fail soft because it has a safety net
    (`rescan_stuck` recovers a lost job) and sits on the ingestion critical
    path, where refusing service costs a client's job order arriving late.
    This counter has no such net, and this endpoint is not on that path — a
    recruiter linking WhatsApp who hits a Redis blip can simply retry once it
    clears. This limiter is also defence-in-depth, not the only wall: RLS
    already confines a guesser to codes issued within their own tenant. Given
    that, the asymmetry is: failing open during an outage hands an attacker
    an *unbounded* guess budget against a live six-digit code for the
    outage's whole duration; failing closed costs a legitimate recruiter a
    delayed retry. The smaller, recoverable harm wins, so a Redis error is
    reported as "already at the limit" rather than "no attempts yet".
    """
    key = _VERIFY_ATTEMPT_KEY.format(user_id=user_id)
    try:
        pool = await redis_pool()
        count = await pool.incr(key)
        if count == 1:
            # Only the request that creates the key sets its TTL — a repeat
            # `expire` on every attempt would keep sliding the window forward
            # and the limit would never actually reset.
            await pool.expire(key, settings.NOTIFY_VERIFY_WINDOW_SECONDS)
        return count
    except Exception:
        log.exception("verify_attempt_count_unavailable", user_id=str(user_id))
        return settings.NOTIFY_VERIFY_MAX_PER_HOUR


@dataclass(frozen=True)
class LinkedToken:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    address: str | None


def generate_code() -> str:
    """A six-digit opt-in code.

    `secrets`, not `random`: this is the only thing standing between a guess
    and a stranger's job orders.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def issue_token(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: str,
    address: str | None = None,
) -> str:
    """Mint a single-use token. Returns the plaintext; stores only its hash.

    The plaintext is returned once and never again. Losing it means issuing
    another, which is cheap — recovering it from the database is meant to be
    impossible.

    Two token shapes on purpose: a six-digit code when `address` is given
    (the WhatsApp opt-in code, sent by SMS/WA where a long token is unwieldy
    to type back), and a 24-byte URL-safe token otherwise (the Telegram deep
    link, which the user never has to transcribe). `token_hash` is UNIQUE, so
    the six-digit space (1e6 values) matters: at this table's realistic
    volume — a handful of live, unexpired tokens per tenant at any moment —
    a same-second collision on the *hash* would require two *codes* to
    collide, which needs a birthday clash inside 1e6 slots among a few
    concurrently-live tokens. That is a real but small probability, not a
    theoretical zero, and is exactly why codes expire quickly
    (`NOTIFY_LINK_TOKEN_TTL_MINUTES`) and are rate-limited per user
    (`opt_in_attempts_this_hour`) rather than relying on the code space alone.
    """
    token = generate_code() if address is not None else secrets.token_urlsafe(24)
    await session.execute(
        _INSERT_TOKEN,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "user_id": user_id,
            "channel": channel,
            "token_hash": _hash_token(token),
            "address_encrypted": encrypt(address) if address else None,
            "expires_at": datetime.now(UTC)
            + timedelta(minutes=settings.NOTIFY_LINK_TOKEN_TTL_MINUTES),
        },
    )
    return token


async def redeem_token(
    session: AsyncSession, token: str, channel: str
) -> LinkedToken | None:
    """Consume a token, or return None if it is spent, expired, or not ours.

    The channel is part of the predicate on purpose: without it a Telegram
    start-token would redeem as a verified phone number.

    This is keyed on the hash alone, with no per-user or per-tenant scoping
    in the predicate — by design, since the caller (a Telegram deep link, a
    WhatsApp webhook reply) doesn't know who it's talking to until the token
    tells it. That is also this function's known weak point for the WhatsApp
    six-digit code: nothing here rate-limits *guesses* against a live code,
    only how often a legitimate user can request one
    (`opt_in_attempts_this_hour`). See the report for the brute-force
    assessment; this function's contract is intentionally unchanged pending
    that decision.
    """
    row = (
        await session.execute(
            _CONSUME_TOKEN,
            {"token_hash": _hash_token(token), "channel": channel},
        )
    ).one_or_none()
    if row is None:
        return None
    return LinkedToken(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        address=decrypt(row.address_encrypted) if row.address_encrypted else None,
    )


async def create_destination(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    channel: str,
    address: str,
) -> uuid.UUID:
    """Record a verified destination, or revive the one already there.

    Upsert rather than insert: re-linking an address someone already had is an
    ordinary thing to do, and a bare insert would turn it into a constraint
    violation with nothing to explain it. Reviving also clears `disabled_at`,
    which is how a recruiter who blocked the bot and changed their mind gets
    notifications back.
    """
    return (
        await session.execute(
            _UPSERT_DESTINATION,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "channel": channel,
                "address_encrypted": encrypt(address),
                "address_hash": address_digest(address),
            },
        )
    ).scalar_one()


# A `whatsapp_linked` destination is a projection of the recruiter's own
# paired device, so when the device moves to a new number the destination must
# follow it — see `reconcile_linked_destination` below. Two statements share
# the same "which row is canonical" rule, stated in one ordering expression:
#
#  1. The row whose `address_hash` already equals the live number wins — it is
#     already the correct projection, and updating it would collide with the
#     `uq_destination_address` unique index if another row somehow held the
#     same hash. Preferring it makes re-pairing back to a previously-used
#     number a no-op instead of a constraint violation.
#  2. Otherwise the most recently *touched* row wins (`updated_at DESC NULLS
#     LAST`), so a row a recruiter just subscribed/unsubscribed through the
#     settings screen beats a dormant sibling; a tie on updated_at (all rows
#     touched in the same transaction) falls back to the newest created, so a
#     row created by a later link attempt wins over the earlier one.
_CANONICAL_LINKED_ORDER = (
    "(address_hash = :hash) DESC, updated_at DESC NULLS LAST, created_at DESC"
)

# Rebind the canonical `whatsapp_linked` row to the live number. The WHERE
# clause mirrors `_CANONICAL_LINKED_ORDER`, so this updates exactly the same
# row that `_CANONICAL_LINKED` would name, and the `address_hash <> :hash`
# guard makes it a no-op when the canonical row already carries the live
# number (including the re-pair-back case, where updating would violate
# `uq_destination_address`). Preserving the row (rather than delete+recreate)
# is what keeps its `notification_subscriptions` rows: a re-paired recruiter
# keeps the events they ticked, now delivered to the new number.
_REBIND_LINKED = text(
    f"""
    UPDATE notification_destinations
    SET address_encrypted = :address_encrypted,
        address_hash = :hash,
        verified_at = now(),
        disabled_at = NULL,
        failure_count = 0,
        updated_at = now()
    WHERE id = (
        SELECT id FROM notification_destinations
        WHERE tenant_id = :tenant_id
          AND user_id = :user_id
          AND channel = :linked_channel
        ORDER BY {_CANONICAL_LINKED_ORDER}
        LIMIT 1
    )
      AND address_hash <> :hash
    """
)

# The canonical row, named by the same rule the UPDATE above used — the UPDATE
# has run by the time this reads, so a freshly-rebound row sorts first under
# the `address_hash = :hash` branch even though its `updated_at` is also the
# newest; the two ordering keys agree.
_CANONICAL_LINKED = text(
    f"""
    SELECT id FROM notification_destinations
    WHERE tenant_id = :tenant_id
      AND user_id = :user_id
      AND channel = :linked_channel
    ORDER BY {_CANONICAL_LINKED_ORDER}
    LIMIT 1
    """
)

# A re-pair never deletes a destination outright: the subscriptions a
# recruiter chose belong to their device, not to one phone number, so a stale
# sibling's subscriptions are moved onto the canonical row before the sibling
# is removed. Without the move, the delete would cascade them away and the
# recruiter would silently stop being told about new job orders after
# switching numbers. `ON CONFLICT DO NOTHING` against `uq_subscription_event`
# keeps an event already subscribed on the canonical row from being duplicated
# by a sibling's move. `active` is copied so an unsubscribed event stays
# unsubscribed; only the destination it hangs off changes.
_MOVE_LINKED_SUBSCRIPTIONS = text(
    """
    INSERT INTO notification_subscriptions
        (id, tenant_id, destination_id, event_kind, active)
    SELECT gen_random_uuid(), s.tenant_id, :canonical_id, s.event_kind, s.active
    FROM notification_subscriptions s
    JOIN notification_destinations d ON d.id = s.destination_id
    WHERE d.tenant_id = :tenant_id
      AND d.user_id = :user_id
      AND d.channel = :linked_channel
      AND d.id <> :canonical_id
    ON CONFLICT (destination_id, event_kind) DO NOTHING
    """
)

_DELETE_STALE_LINKED = text(
    """
    DELETE FROM notification_destinations
    WHERE tenant_id = :tenant_id
      AND user_id = :user_id
      AND channel = :linked_channel
      AND id <> :canonical_id
    """
)


async def reconcile_linked_destination(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, phone_e164: str
) -> None:
    """Point the recruiter's `whatsapp_linked` destination at their current
    number, and collapse any duplicates created by earlier link attempts.

    Called by the WA session's single status writer (`apply_internal_status` in
    `app/api/wa_gateway.py`) whenever the gateway reports a `connected` session
    with a phone number. A paired device is the source of truth for "what
    number does this recruiter get notifications on"; the destination row is a
    projection of it. Leaving the projection stale is exactly the bug where a
    recruiter who re-pairs with a second number keeps getting notifications on
    the first: the row still pointed at number one, so the send went there.

    Deliberately idempotent and a no-op when there is nothing to do: no row,
    or a row already pointing at `phone_e164`, changes nothing. Runs inside
    the caller's `tenant_session`, so it commits (or rolls back) with the
    status write that triggered it.

    Only ever called with a *connected* status carrying a real phone (the
    `apply_internal_status` guard), so `phone_e164` is non-empty here; the
    signature stays a plain `str` and trusts that invariant rather than
    re-checking it.
    """
    await session.execute(
        _REBIND_LINKED,
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "linked_channel": CHANNEL_WHATSAPP_LINKED,
            "address_encrypted": encrypt(phone_e164),
            "hash": address_digest(phone_e164),
        },
    )
    canonical_id = (
        await session.execute(
            _CANONICAL_LINKED,
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "linked_channel": CHANNEL_WHATSAPP_LINKED,
                "hash": address_digest(phone_e164),
            },
        )
    ).scalar_one_or_none()
    if canonical_id is None:
        return
    await session.execute(
        _MOVE_LINKED_SUBSCRIPTIONS,
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "linked_channel": CHANNEL_WHATSAPP_LINKED,
            "canonical_id": canonical_id,
        },
    )
    await session.execute(
        _DELETE_STALE_LINKED,
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "linked_channel": CHANNEL_WHATSAPP_LINKED,
            "canonical_id": canonical_id,
        },
    )


async def opt_in_attempts_this_hour(
    session: AsyncSession, user_id: uuid.UUID, channel: str
) -> int:
    """How many codes this user has asked us to send.

    Sending an authentication template to any number a user types is an OTP
    pump aimed at our WABA's reputation, and the reputation is shared by every
    tenant on the number.
    """
    return (
        await session.execute(
            _OPT_IN_ATTEMPTS, {"user_id": user_id, "channel": channel}
        )
    ).scalar_one()
