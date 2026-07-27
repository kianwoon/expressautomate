"""OpenRouter JSON completion (plan §32).

Returns parsed data or raises. It never repairs a malformed response beyond
stripping a code fence, and never falls back to a default value — a silent
default here becomes a fabricated salary in someone's database.

Like the Graph client, it classifies rather than retries: `LLMInvalidJSON` says
"this response is unusable", and everything else is left as the httpx error it
already is. The job layer is the only place that knows whether re-asking a model
is worth the tokens, so it owns retry and escalation to the strong model.
"""

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

    `base_url` and `api_key` default to OpenRouter's, so extraction is
    unchanged. They exist because the relevance gate runs on Cerebras — a
    second provider, not a second client: the wire format is the same
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

    async with httpx.AsyncClient(
        base_url=base_url or settings.LLM_BASE_URL,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        transport=transport,
        headers={"Authorization": f"Bearer {api_key or settings.OPENROUTER_API_KEY}"},
    ) as client:
        response = await client.post("/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()

    # Every hop is optional, because every one of them has been absent from a
    # real response: a reasoning model that spent its budget thinking returns
    # a message with a `reasoning` key and no `content` at all. Indexing would
    # raise KeyError, which reads as a bug in this module rather than as the
    # unusable answer it is — and the gate's caller only handles the latter.
    choices = body.get("choices") or [{}]
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise LLMInvalidJSON("the model returned no content")
    usage = body.get("usage") or {}
    return LLMResult(
        data=_parse(content),
        # OpenRouter may route to a different model than the one requested.
        # Recording what answered, not what we asked for, is what makes a
        # per-model quality comparison mean anything.
        model=body.get("model", model),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        latency_ms=int((time.monotonic() - started) * 1000),
        raw=body,
    )


def _parse(content: str) -> dict:
    if match := _FENCE.match(content or ""):
        content = match.group(1)
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        # Truncated: the message ends up in logs, and a runaway completion would
        # otherwise put a whole email body there.
        raise LLMInvalidJSON(content[:500]) from exc
    if not isinstance(parsed, dict):
        # A bare list or number parses fine and would then fail far downstream
        # on an attribute the caller assumed. Reject it where it happened.
        raise LLMInvalidJSON(f"expected an object, got {type(parsed).__name__}")
    return parsed


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
