"""Gate cost provenance — one `classification_usages` row per verdict.

The gate is the highest-volume LLM call in the system and, until this module
existed, the only one with no recorded spend: `extractions` keeps prompt and
completion tokens per extraction, but nothing answered "what did the gate
cost per email". Cost planning is guesswork without a before/after, so the
jobs that call the gate write one row per verdict here — including the
fail-open `uncertain` verdicts, because a gate call that answered nothing
still billed tokens.

The write lives in its own module (not in classify.py) because the gate
service itself has no database access: it is a pure prompt/response function
whose callers — the single-email and batch jobs — own persistence. Recording
usage is a persistence concern, so it belongs with the callers.

`record_classification_usage` is deliberately fire-and-forget from the
callers' perspective: it takes the session the caller already holds, so the
row commits with the same transaction that records the verdict. A failure
here must not lose a verdict, so callers catch and log rather than propagate.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

# allow-hardcode: SQL statement, not a phrase list.
_INSERT_USAGE = text(
    """
    INSERT INTO classification_usages
        (id, tenant_id, email_message_id, model_name, prompt_version,
         prompt_tokens, completion_tokens, latency_ms)
    VALUES (:id, :tenant_id, :email_message_id, :model_name, :prompt_version,
            :prompt_tokens, :completion_tokens, :latency_ms)
    """
)


async def record_classification_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    model_name: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    latency_ms: int | None,
) -> None:
    """Write one gate-usage row inside the caller's transaction.

    `prompt_version` is taken from settings here rather than passed, so a
    caller cannot accidentally record a verdict under a version it did not
    actually use — the version is a property of the deployment, not of the
    call.
    """
    await session.execute(
        _INSERT_USAGE,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "email_message_id": email_message_id,
            "model_name": model_name,
            "prompt_version": settings.PROMPT_VERSION,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
        },
    )
