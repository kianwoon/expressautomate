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
from app.models.notification import address_digest

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

_UPSERT_DESTINATION = text(
    """
    INSERT INTO notification_destinations
        (id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at)
    VALUES (:id, :tenant_id, :user_id, :channel, :address_encrypted, :address_hash, now())
    ON CONFLICT (tenant_id, channel, address_hash) DO UPDATE
      SET verified_at = now(), disabled_at = NULL, failure_count = 0
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
