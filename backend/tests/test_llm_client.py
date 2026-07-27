import httpx
import pytest

from app.services.llm.client import LLMInvalidJSON, complete_json


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
