"""The LLM client, against a fake transport — never a real model.

allow-hardcode: the base URL below is a test fixture, not configuration. It is
deliberately *not* read from settings: see `_own_base_url`.
"""

import asyncio

import httpx
import pytest

from app.core.config import settings
from app.services.llm.client import LLMInvalidJSON, LLMNoContent, complete_json


@pytest.fixture(autouse=True)
def _own_base_url(monkeypatch):
    """Give these tests a base URL of their own.

    Without it they passed locally, where the repo `.env` supplies
    `LLM_BASE_URL`, and failed in CI, where nothing does: httpx then builds a
    hostless URL and raises `unknown url type: '/chat/completions'` — the same
    failure that took `GRAPH_BASE_URL` a production afternoon to find.

    A test that only passes because of an untracked file is testing the file.
    The mock transport intercepts the request, so the value is never dialled;
    what matters is only that a URL can be formed.
    """
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://llm.test/v1")


def _transport(payload, status=200):
    return httpx.MockTransport(lambda r: httpx.Response(status, json=payload))


async def test_returns_parsed_json_and_usage():
    payload = {
        "choices": [{"message": {"content": '{"jobs": []}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"jobs": []}
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 20
    assert result.latency_ms >= 0


async def test_non_json_content_raises_rather_than_guessing():
    payload = {
        "choices": [{"message": {"content": "Sure! Here are the jobs:"}}],
        "usage": {},
        "model": "test/fast",
    }

    with pytest.raises(LLMInvalidJSON):
        await complete_json(
            "prompt", model="test/fast", schema={}, transport=_transport(payload)
        )


async def test_json_wrapped_in_a_code_fence_is_recovered():
    """Models do this constantly; failing on it wastes a retry and a strong-model call."""
    payload = {
        "choices": [{"message": {"content": '```json\n{"jobs": []}\n```'}}],
        "usage": {},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"jobs": []}


async def test_glm_answer_envelope_is_unwrapped():
    """GLM's coding plan wraps every answer in `{"answer": {...}}` regardless
    of what the prompt asks for — its injected system prompt forces it. A flat
    response only happens by accident. Without unwrapping, every downstream
    schema validation fails on the wrapper dict (`{'answer': {...}}` is not an
    `OccupationProfile`)."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"answer": {"occupation": "Logistics Manager",'
                        ' "seniority": "Manager", "people_management": true,'
                        ' "industry": "Logistics", "functions": {"Ops": 100}}}'
                    )
                }
            }
        ],
        "usage": {},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data["occupation"] == "Logistics Manager"
    assert result.data["industry"] == "Logistics"
    assert "answer" not in result.data


async def test_glm_answer_envelope_as_string_is_unwrapped():
    """Sometimes GLM returns the answer as a JSON *string* inside the
    envelope: `{"answer": "<json>"}` instead of `{"answer": {...}}`. The
    parser must parse the inner string too."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"answer": "{\\"communication_style\\": \\"Direct\\",'
                        ' \\"career_stage\\": \\"Mid-level\\"}"}'
                    )
                }
            }
        ],
        "usage": {},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data["communication_style"] == "Direct"
    assert result.data["career_stage"] == "Mid-level"
    assert "answer" not in result.data


async def test_empty_content_raises_llm_no_content_not_a_generic_error():
    """An empty `content` — a reasoning model that spent its whole budget
    thinking — is the specific exception the job layer retries. It must arrive
    as `LLMNoContent`, not as the generic `LLMInvalidJSON`, or the retry never
    fires and the analysis fails for good on the first empty response."""
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "reasoning": "long chain of thought that used the whole budget",
                    "content": None,
                }
            }
        ],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 16000}},
        "model": "test/fast",
    }

    with pytest.raises(LLMNoContent):
        await complete_json(
            "prompt", model="test/fast", schema={}, transport=_transport(payload)
        )


async def test_a_tls_hang_surfaces_as_retryable_timeout_not_cancelled():
    """Pins the production outage (arq log 2026-08-24).

    A TLS-layer hang in anyio's `receive()` surfaces as `CancelledError` — a
    BaseException that bypasses the old `except _RETRYABLE` clause entirely.
    It escaped `complete_json` uncaught, arq's own 300 s job timeout cancelled
    the job from outside, and the whole 3-attempt transport retry loop never
    fired. Now the hang is retried as if it were a timeout, and once the
    retries are spent the job layer receives a `TimeoutError` — the shape
    `extract()` already knows how to escalate from.
    """
    calls = {"n": 0}

    def hang(request):
        calls["n"] += 1
        # A real TLS hang in anyio blocks on an asyncio.Event that never
        # fires; when arq's outer wait_for(300 s) cancels the chain, that
        # blocked await raises CancelledError. The mock raises the same
        # BaseException subclass straight out of the transport handler so the
        # retry loop sees exactly what the production hang produces.
        raise asyncio.CancelledError

    with pytest.raises(TimeoutError):
        await complete_json(
            "prompt",
            model="test/fast",
            schema={},
            transport=httpx.MockTransport(hang),
        )
    # All three transport attempts were made before giving up — the loop that
    # used to be skipped entirely by the uncaught cancellation.
    assert calls["n"] == 3


async def test_a_real_external_cancellation_still_propagates():
    """The CancelledError catch is for TLS hangs, not for shutdown.

    When arq (or the worker) cancels the job on purpose — shutdown, an abort,
    its own job timeout — the cancellation must keep propagating so the job is
    re-enqueued rather than silently converted into a permanent failure.
    `complete_json` must only ever convert the cancellation it raised itself
    for a hang, never one raised from outside.
    """
    async def never_returns():
        await asyncio.Event().wait()

    task = asyncio.create_task(never_returns())
    await asyncio.sleep(0)  # let it start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

