"""OpenAI-compatible JSON completion (plan §32).

Calls the configured LLM provider (settings.LLM_PROVIDER_* — DeepInfra today)
on its /chat/completions endpoint. DeepInfra serves the DeepSeek models under
full ids (`deepseek-ai/DeepSeek-V4-Flash-0731`), so model names carry the
`deepseek-ai/` prefix there.

Returns parsed data or raises. It never repairs a malformed response beyond
stripping a code fence, and never falls back to a default value — a silent
default here becomes a fabricated salary in someone's database.

Like the Graph client, it classifies rather than retries: `LLMInvalidJSON` says
"this response is unusable", and everything else is left as the httpx error it
already is. The job layer is the only place that knows whether re-asking a model
is worth the tokens, so it owns retry and escalation to the strong model.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

# Anchored to the whole string: a fence is a wrapper the model added around its
# answer, not a thing to go hunting for mid-document. Matching loosely would let
# prose *containing* a fenced example pass as the answer itself.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMInvalidJSON(Exception):
    """The model did not return parseable JSON.

    Separate from a transport error because the caller's answer differs: a
    timeout is worth retrying unchanged, this is worth re-asking a stronger
    model — or giving up and flagging the email for a human.
    """


class LLMNoContent(LLMInvalidJSON):
    """The model returned a reasoning trace and no answer at all.

    Distinct from a bad answer: a malformed JSON answer is the model's best
    effort, and re-asking it at temperature zero is "the same answer twice".
    This is the model never emitting anything — a reasoning model that spent
    its whole output budget thinking (DeepSeek counts reasoning tokens against
    `max_tokens`). Re-asking is a materially different request — the reasoning
    trace may land differently and the budget may have grown — so a job layer
    may retry this without violating the no-retry rule for real answers.
    """


class LLMResponseTruncated(LLMInvalidJSON):
    """The provider reported `finish_reason=length` and the answer did not parse.

    Separate subclass, not a plain `LLMInvalidJSON`, because the remedy differs
    from every other malformed answer. Truncation is not the model guessing
    wrongly — it is the output budget ending before the answer did, and a
    reasoning model spends thinking tokens out of that same budget. Re-asking
    under a grown (or less reasoning-hungry) configuration asks a materially
    different question, so a caller may retry this the way it retries
    `LLMNoContent`, unlike an ordinary invalid answer.
    """


@dataclass
class LLMResult:
    data: dict
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    # The untouched response body. Kept so a bad extraction can be diagnosed
    # from what the model actually said, not from our summary of it.
    raw: dict = field(default_factory=dict)


async def complete_json(
    prompt: str,
    *,
    model: str,
    schema: dict | None,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    extra_body: dict | None = None,
) -> LLMResult:
    """Ask `model` for one JSON object.

    `transport` is the seam tests use; nothing in production passes it, and it
    is what keeps the suite from ever spending money on a real completion.

    `base_url` and `api_key` default to the configured LLM provider's, so
    extraction is unchanged. They exist because callers may target a second
    provider, not a second client: the wire format is the same
    OpenAI-compatible one, and duplicating this module to change two strings
    would mean the next fix to response handling landing in only one of them.

    `schema=None` asks for a bare JSON object instead of a named JSON schema.
    Not every provider implements `json_schema`, and a request rejected for an
    unsupported `response_format` fails every email identically.

    `extra_body` is merged last so a caller can send provider-specific
    parameters — `reasoning_effort` for the gate's model, which without it
    spends its whole token budget reasoning and returns no content at all.
    """
    started = time.monotonic()
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # The caller's schema, actually sent. It was previously accepted as an
        # argument and dropped here, so the structure extraction depends on —
        # a value, its quotation, and the offsets that make it checkable — was
        # never asked for; the parser then rejected responses for missing
        # fields the model was never told about.
        "response_format": (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    # The provider enforces the shape rather than hoping the
                    # prose in the prompt was persuasive.
                    "strict": True,
                    "schema": schema,
                },
            }
            if schema
            else {"type": "json_object"}
        ),
        # Extraction must be reproducible: the same email replayed through the
        # same prompt version has to give the same answer, or a corrections
        # table can never be trusted as a baseline.
        "temperature": 0,
    }
    payload.update(extra_body or {})

    # Transport errors — a connection reset or a stream interruption — are
    # transient. An HTTP status error is not: a 429 or 503 is the provider's
    # answer, and retrying it is the job layer's call, not this module's.
    #
    # asyncio.TimeoutError is included because httpx's internal timeout can be
    # bypassed by a TLS-layer hang in anyio (the SSL read blocks on a bare
    # asyncio.Event that neither httpcore's nor anyio's cancel scopes reach).
    # The wait_for wrapper below catches those hangs and turns them into a
    # retryable exception instead of letting arq's job timeout kill the task
    # from the outside.
    #
    # CancelledError must be caught here too: it is what that same TLS hang
    # surfaces as in older anyio (the `receive()` path cancels the caller rather
    # than raising TimeoutError). It is a subclass of BaseException, so it
    # bypasses the `except _RETRYABLE` clause and escapes `complete_json` as an
    # abrupt cancellation. arq's own timeout then cancels the job from outside
    # (the `300.00s ! extract_email failed, TimeoutError` in the arq log), the
    # whole 3-attempt retry loop never fires, and the strong-model escalation
    # in `extract()` never gets a chance. Catching it here turns a hang into
    # the same retryable provider-hang path the wait_for wrapper already
    # serves — and, when the retries are spent, into a TimeoutError (see the
    # re-raise below; a CancelledError escaping a task marks the task
    # cancelled, which arq would report as "cancelled, will be run again" and
    # re-enqueue even after the attempts are gone). Only the
    # `asyncio.CancelledError` alias is caught — anything else the job layer
    # cancelled on purpose (worker shutdown, arq's timeout) must keep
    # propagating so shutdown is not silently absorbed.
    _RETRYABLE = (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
        asyncio.TimeoutError,
        asyncio.CancelledError,
    )
    # CancelledError is a BaseException, not an Exception, so the variable must
    # be typed for both: it is what a TLS hang surfaces as and it is retried.
    last_exc: BaseException | None = None
    _timeout = settings.LLM_TIMEOUT_SECONDS

    # The provider every production call site names explicitly: DeepInfra
    # today (see settings.LLM_PROVIDER_*). The legacy OpenRouter pair below it
    # is the fallback so a deployment that has not migrated its env yet still
    # answers through the router rather than against a hostless URL.
    provider_base_url = base_url or settings.LLM_PROVIDER_BASE_URL or settings.LLM_BASE_URL
    provider_api_key = api_key or settings.LLM_PROVIDER_API_KEY or settings.OPENROUTER_API_KEY
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                base_url=provider_base_url,
                timeout=_timeout,
                transport=transport,
                headers={"Authorization": f"Bearer {provider_api_key}"},
            ) as client:
                response = await asyncio.wait_for(
                    client.post("/chat/completions", json=payload),
                    timeout=_timeout,
                )
                response.raise_for_status()
                body = response.json()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(2**attempt)
                continue
            if isinstance(exc, asyncio.CancelledError):
                # The retries are spent on a hang that surfaced as
                # CancelledError. It must leave this function as a TimeoutError
                # (the shape `extract()` escalates from) rather than as a raw
                # CancelledError: a CancelledError escaping a task marks the
                # task cancelled, so arq would report "cancelled, will be run
                # again" and re-enqueue the job even though we already spent
                # all three transport attempts on it — the same lost retry the
                # uncaught hang caused, one hop later. `raise ... from` keeps
                # the original as the chain's cause for the log.
                raise TimeoutError("LLM provider hang: retries exhausted") from exc
            raise
        else:
            break
    else:
        raise last_exc  # type: ignore[misc]

    # Every hop is optional, because every one of them has been absent from a
    # real response: a reasoning model that spent its budget thinking returns
    # a message with a `reasoning` key and no `content` at all. Indexing would
    # raise KeyError, which reads as a bug in this module rather than as the
    # unusable answer it is — and the gate's caller only handles the latter.
    choices = body.get("choices") or [{}]
    choice = choices[0] or {}
    content = (choice.get("message") or {}).get("content")
    finish_reason = choice.get("finish_reason")
    if not content:
        raise LLMNoContent("the model returned no content")
    usage = body.get("usage") or {}
    try:
        data = _parse(content)
    except LLMInvalidJSON as exc:
        # A parse failure here carries its diagnosis with it, or the failure
        # stays a mystery: was the answer truncated at the token budget
        # (`finish_reason=length` — expected on a reasoning model that spent
        # everything thinking), or did the model emit well-formed-looking but
        # broken JSON on an unbounded completion? The first production case
        # (job intelligence search stage, 2026-08-27 05:29Z) showed a string
        # cut mid-word, but the error's own [:500] preview could not say where
        # the content ended and the log carried no budget information.
        #
        # `_truncate_reason` turns those two signals into one sentence and
        # re-raises as `LLMResponseTruncated` when length was the cause, so a
        # caller can tell "grow the budget / dial reasoning down" apart from
        # "re-ask or escalate to a stronger model".
        truncate_note = _truncate_reason(finish_reason, content)
        message = f"{exc}; {truncate_note}"
        if finish_reason == "length":
            raise LLMResponseTruncated(message) from exc
        raise type(exc)(message) from exc
    return LLMResult(
        data=data,
        # OpenRouter may route to a different model than the one requested.
        # Recording what answered, not what we asked for, is what makes a
        # per-model quality comparison mean anything.
        model=body.get("model", model),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        latency_ms=int((time.monotonic() - started) * 1000),
        raw=body,
    )


def _truncate_reason(finish_reason: str | None, content: str) -> str:
    """One diagnostic sentence about *where* a malformed answer stopped.

    `length` means the output budget cut it — the useful number is how much
    of the budget the answer consumed (completion tokens land in the same
    body). Any other reason names itself; an absent one reads as unknown so
    the log never invents a cause. The tail matters when the defect hides
    past an error's [:500] head: 'ends mid-string' vs 'closes cleanly then
    breaks' points at truncation versus malformation.
    """
    tail = content[-120:].replace("\n", "\\n")
    if finish_reason == "length":
        return (
            f"provider reported finish_reason=length "
            f"(output budget exhausted; ends mid-content: ...{tail!r})"
        )
    if finish_reason:
        return f"finish_reason={finish_reason} (ends: ...{tail!r})"
    return f"no finish_reason in response (ends: ...{tail!r})"


def _parse(content: str) -> dict:
    if match := _FENCE.match(content or ""):
        content = match.group(1)
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # GLM's coding plan sometimes returns the answer envelope as MALFORMED
        # JSON: `{"answer": "<pretty-printed inner JSON with raw newlines and
        # unescaped quotes>"}`. The outer `json.loads` fails on the control
        # characters before we ever see the envelope, so recover by extracting
        # the inner JSON document and parsing it directly.
        rescued = _rescue_answer_envelope(content)
        if rescued is not None:
            return rescued
        # Truncated: the message ends up in logs, and a runaway completion would
        # otherwise put a whole email body there.
        raise LLMInvalidJSON(content[:500]) from None
    if not isinstance(parsed, dict):
        # A bare list or number parses fine and would then fail far downstream
        # on an attribute the caller assumed. Reject it where it happened.
        raise LLMInvalidJSON(f"expected an object, got {type(parsed).__name__}")
    # GLM's coding plan wraps every answer in an envelope: the model returns
    # `{"answer": {...}}` or `{"answer": "<json string>"}` or, less often,
    # `{"answer": [...]}`. Its injected system prompt forces this shape
    # regardless of what the caller's prompt asks for, so a flat response
    # only happens by accident. Unwrap it when present; otherwise every
    # schema validation fails on the wrapper dict (input_value={'answer': ...}
    # is not an OccupationProfile).
    #
    # The envelope is not always the only key — GLM occasionally adds a
    # top-level `confidence` beside it (`{"answer": ..., "confidence": 0.05}`,
    # production log 2026-08-26). Unwrap on the presence of `answer` rather
    # than requiring a single-key dict, so the extra field cannot defeat the
    # unwrap and send the wrapper to the caller's schema.
    if "answer" in parsed:
        answer = parsed["answer"]
        if isinstance(answer, dict):
            return answer
        if isinstance(answer, list):
            # GLM sometimes returns a list inside the envelope instead of an
            # object — the model inferred an array response. The caller is
            # expecting a dict, so this is genuinely wrong, but failing with
            # "got list" here is less helpful than letting the caller's own
            # schema validation reject it. Return the list as-is and let the
            # pydantic model raise the meaningful error.
            raise LLMInvalidJSON(
                f"expected an object inside the answer envelope, "
                f"got a list with {len(answer)} items"
            )
        if isinstance(answer, str):
            # The answer is a JSON document inside the envelope string. Strip
            # a code fence if the model wrapped the string, then re-parse.
            if match := _FENCE.match(answer):
                answer = match.group(1)
            try:
                inner = json.loads(answer)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LLMInvalidJSON(answer[:500]) from exc
            if isinstance(inner, dict):
                return inner
            raise LLMInvalidJSON(
                f"expected an object inside the answer envelope, got {type(inner).__name__}"
            )
    return parsed


def _rescue_answer_envelope(content: str) -> dict | None:
    """Recover a GLM answer envelope whose outer JSON is malformed.

    The model sometimes returns `{"answer":"\n{\n  \"roles\": [\n  ... \n}"}`
    — an `answer` key holding a pretty-printed JSON document with raw
    newlines and unescaped quotes. That is invalid JSON (control characters
    in a string), so the normal `json.loads` path fails before the envelope
    can be unwrapped. This finds the inner document between the `"answer":"`
    marker and the closing `"}` and parses it directly.

    Returns the parsed inner dict, or None when the content does not match
    this pattern (so the caller raises the ordinary invalid-JSON error).
    """
    marker = '"answer":"'
    start = content.find(marker)
    if start == -1:
        return None
    start += len(marker)
    # The envelope ends with `"}` — the last `"}` in the content (the inner
    # document's own closing braces are inside, so the final `"}` is the
    # wrapper's). If the inner document has a `"` before its end this could
    # cut early, but the common failure is precisely a document whose closing
    # quotes are unterminated, so the last `"}` is the reliable boundary.
    end = content.rfind('"}')
    if end <= start:
        return None
    inner_text = content[start:end]
    if match := _FENCE.match(inner_text):
        inner_text = match.group(1)
    try:
        inner = json.loads(inner_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(inner, dict):
        return inner
    return None


class FakeLLM:
    """Test double. Queue responses; assert on the prompts it received.

    Substitutable for `complete_json` because it is callable with the same
    signature — a test swaps it in without the code under test knowing.
    """

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, *, model: str, schema: dict, **_) -> LLMResult:
        self.prompts.append(prompt)
        if not self.responses:
            # Loud rather than returning an empty dict: a silently-empty
            # extraction looks exactly like an email with no job in it.
            raise AssertionError("FakeLLM ran out of queued responses")
        return LLMResult(data=self.responses.pop(0), model=model)
