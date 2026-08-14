"""Recruitment relevance gate (plan Task 4).

A recruiter's mailbox is mostly not job orders, and extracting from every
message costs money, storage, and privacy exposure for nothing. This gate is a
cheap model answering one yes/no question.

It fails **open**. An uncertain or broken classification proceeds to extraction:
the cost of a wasted extraction call is a fraction of a cent, and the cost of a
silently dropped job order is a vacancy the recruiter never sees and never
knows to look for. That asymmetry is the whole design, so every error path here
converges on `uncertain` rather than raising.
"""

from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger
from app.services.llm.client import complete_json

log = get_logger(__name__)

# The JSON schema that used to live here is gone with the move off the router:
# both paths ask for a bare JSON object, which is the response format every
# OpenAI-compatible endpoint implements. The shape is asked for in the prompt
# and — crucially — checked on the way out, which is what actually protects
# against a bad answer.
# allow-hardcode: a prompt, not configuration.
PROMPT = """Decide whether this email is a recruitment job order — a message
describing one or more vacancies a recruiter is being asked to fill.

It IS a job order if it describes a role to hire for: a title, requirements,
salary, or a client asking for candidates.

It is NOT a job order if it is an invoice, a candidate's application or CV
submission, a meeting invite, a newsletter, or ordinary correspondence.

Return JSON: {{"is_job_order": true|false, "reason": "<one short sentence>"}}

EMAIL:
{email}
"""

# The batch prompt asks for the index back with every verdict. Verdicts in bare
# positional order would look identical whether the model answered about email
# 3 or skipped it — and a misaligned verdict is worse than a missing one: it is
# a confident wrong answer on the wrong email, with no way to detect it.
BATCH_PROMPT = """Decide, for EACH email below, whether it is a recruitment job
order — a message describing one or more vacancies a recruiter is being asked
to fill.

An email IS a job order if it describes a role to hire for: a title,
requirements, salary, or a client asking for candidates.

It is NOT a job order if it is an invoice, a candidate's application or CV
submission, a meeting invite, a newsletter, or ordinary correspondence.

Return one verdict per email, echoing the email's index exactly as given.
Return every index, even if you are unsure. Return JSON:
{{"verdicts": [{{"index": 0, "is_job_order": true, "reason": "<one short sentence>"}}]}}

{emails}
"""

# allow-hardcode: the delimiter of the batch prompt, not configuration.
_EMAIL_BLOCK = "--- EMAIL index={index} ---\n{text}"

# `classification_reason` is Text but the column is read by humans, not parsed.
# Truncating keeps one verbose model — or one long exception repr — from
# turning a diagnostic column into a storage line item.
_REASON_LIMIT = 500


@dataclass
class Classification:
    status: str
    reason: str
    model: str
    # Cost provenance, carried from the LLMResult so the jobs can persist it.
    # None for a fail-open `uncertain` where the model never answered, and for
    # a caller that does not need it — the dataclass stays backward compatible.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


def should_extract(status: str) -> bool:
    """Anything but a confident 'no' goes on to extraction."""
    return status != "non_recruitment"


async def classify(text: str, llm=None) -> Classification:
    """`llm` defaults to None, not to `complete_json`.

    A default argument binds at definition time, so `llm=complete_json` would
    capture the function object and make monkeypatching this module's
    `complete_json` do nothing — the test would pass through to a real HTTP
    call. Resolving at call time is what makes the fake reachable.
    """
    resolve = llm or complete_json
    model = settings.CLASSIFIER_MODEL
    try:
        # The same provider and parameters the batch path uses. A single-email
        # recovery that asked a different endpoint for the same model id would
        # 404 per email, and only ever on the recovery path — the one nobody
        # exercises until something has already gone wrong.
        result = await resolve(
            PROMPT.format(email=text[: settings.CLASSIFIER_CHARS_PER_EMAIL]),
            model=model,
            schema=None,
            base_url=settings.LLM_PROVIDER_BASE_URL,
            api_key=settings.LLM_PROVIDER_API_KEY,
            extra_body={
                "max_tokens": settings.CLASSIFIER_MAX_TOKENS,
                "reasoning_effort": settings.CLASSIFIER_REASONING_EFFORT,
            },
        )
        verdict = result.data.get("is_job_order")
        # Not `bool(verdict)`: a missing key would read as False, which is the
        # one wrong answer that discards the email. An absent verdict is a
        # broken response, and broken responses fail open.
        if not isinstance(verdict, bool):
            raise ValueError(f"missing is_job_order in {result.data!r}")
        return Classification(
            status="recruitment" if verdict else "non_recruitment",
            reason=str(result.data.get("reason", ""))[:_REASON_LIMIT],
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
        )
    except Exception as exc:
        # Deliberately bare. Transport errors, invalid JSON and malformed
        # verdicts all have the same right answer here, and enumerating them
        # would mean a new exception type in the client silently becoming a
        # failed job instead of an extra extraction call.
        log.warning("classification_failed_open", error=repr(exc))
        return _uncertain(f"gate failed: {exc}", model)


def _uncertain(
    reason: str,
    model: str,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
) -> Classification:
    return Classification(
        status="uncertain",
        reason=reason[:_REASON_LIMIT],
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )


async def classify_many(texts: list[str], llm=None) -> list[Classification]:
    """Classify a whole batch in one call, in the order given.

    The gate is the highest-volume call in the system, and its per-call
    overhead — the instructions above, repeated verbatim — is most of what a
    single-email call pays for. Batching pays it once.

    The return is always the same length as `texts`. That is the contract the
    caller depends on: a batch is a shared fate for the *call*, never for the
    verdicts. One element the model omitted, mislabelled, or answered with
    something that is not a boolean falls open to `uncertain` on its own, and
    the rest of the batch keeps its answers. Dropping it instead would lose a
    job order without anything recording that an email went missing.
    """
    resolve = llm or complete_json
    model = settings.CLASSIFIER_MODEL
    if not texts:
        return []

    limit = settings.CLASSIFIER_CHARS_PER_EMAIL
    body = "\n\n".join(
        _EMAIL_BLOCK.format(index=i, text=text[:limit]) for i, text in enumerate(texts)
    )

    try:
        result = await resolve(
            BATCH_PROMPT.format(emails=body),
            model=model,
            # No JSON schema: the gate's provider is asked for a bare JSON
            # object, which is the response format every OpenAI-compatible
            # endpoint implements.
            schema=None,
            base_url=settings.LLM_PROVIDER_BASE_URL,
            api_key=settings.LLM_PROVIDER_API_KEY,
            extra_body={
                "max_tokens": settings.CLASSIFIER_MAX_TOKENS,
                "reasoning_effort": settings.CLASSIFIER_REASONING_EFFORT,
            },
        )
    except Exception as exc:
        # The call itself failed, so nothing in the batch has an answer. Each
        # email fails open individually rather than the job raising: a raise
        # here would leave the whole batch to a retry that costs the same
        # tokens for the same likely failure.
        log.warning("batch_classification_failed_open", error=repr(exc), size=len(texts))
        return [_uncertain(f"gate failed: {exc}", model) for _ in texts]

    verdicts = _by_index(result.data)
    out: list[Classification] = []
    for i in range(len(texts)):
        entry = verdicts.get(i)
        is_job_order = (entry or {}).get("is_job_order")
        # Not `bool(...)`: a missing key would read as False, which is the one
        # wrong answer that discards the email.
        if not isinstance(is_job_order, bool):
            out.append(
                _uncertain(
                    f"no usable verdict for index {i}",
                    result.model,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_ms=result.latency_ms,
                )
            )
            continue
        out.append(
            Classification(
                status="recruitment" if is_job_order else "non_recruitment",
                reason=str(entry.get("reason", ""))[:_REASON_LIMIT],
                model=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
            )
        )

    missing = sum(1 for c in out if c.status == "uncertain")
    if missing:
        # Worth a line: a model that keeps skipping entries is a batch size to
        # lower, and the symptom otherwise is only a rise in extraction spend.
        log.warning("batch_verdicts_incomplete", missing=missing, size=len(texts))
    return out


def _by_index(data: dict) -> dict[int, dict]:
    """Index the model's verdicts, tolerating everything except ambiguity.

    A verdict whose index is absent, not an integer, or out of range is
    dropped rather than guessed at, which lands its email on `uncertain` — the
    same place a missing verdict lands. Guessing would risk pairing a verdict
    with the wrong email, and `non_recruitment` on the wrong email is a job
    order silently thrown away.
    """
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        return {}
    out: dict[int, dict] = {}
    for entry in verdicts:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        # `bool` is an `int` in Python, and `{"index": true}` would otherwise
        # silently claim email 1.
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        out[index] = entry
    return out
