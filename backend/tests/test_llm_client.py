"""The LLM client, against a fake transport — never a real model.

allow-hardcode: the base URL below is a test fixture, not configuration. It is
deliberately *not* read from settings: see `_own_base_url`.
"""

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
