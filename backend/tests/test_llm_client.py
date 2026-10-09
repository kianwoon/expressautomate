"""The LLM client, against a fake transport — never a real model.

allow-hardcode: the base URL below is a test fixture, not configuration. It is
deliberately *not* read from settings: see `_own_base_url`.
"""

import asyncio

import httpx
import pytest

from app.core.config import settings
from app.services.llm.client import (
    LLMInvalidJSON,
    LLMNoContent,
    LLMResponseTruncated,
    LLMSyntaxError,
    complete_json,
)


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
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))


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


async def test_glm_answer_envelope_with_extra_key_is_unwrapped():
    """GLM sometimes adds a top-level `confidence` beside the answer envelope
    (`{"answer": "<json string>", "confidence": 0.05}`, production log
    2026-08-26). The unwrap must fire on the presence of `answer` — not only
    when it is the sole key — or the wrapper dict reaches the caller's schema
    and JDUnderstanding fails on missing role/business_purpose/etc."""
    import json as _json

    inner = {
        "role": "DMS Executive",
        "business_purpose": "Handle DMS operations",
        "work_environment": "Office",
        "working_conditions": "Onsite 5 days",
    }
    content = _json.dumps({"answer": _json.dumps(inner), "confidence": 0.05})
    payload = {
        "choices": [{"message": {"content": content}}],
        "usage": {},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data["role"] == "DMS Executive"
    assert result.data["business_purpose"] == "Handle DMS operations"
    assert "answer" not in result.data
    assert "answer" not in result.data


async def test_glm_answer_envelope_malformed_json_is_rescued():
    """GLM sometimes returns the answer envelope as invalid JSON — the
    inner JSON document is pretty-printed with raw newlines and unescaped
    quotes inside the string value. The outer `json.loads` fails, but the
    inner document can be extracted and parsed."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"answer":"\n{\n  "roles": [\n'
                        '    {"title":{"value":"Software Engineer",'
                        '"evidence":"Engineer","start_char":0,"end_char":8,'
                        '"confidence":0.95}}\n  ]\n}"}'
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

    assert result.data["roles"][0]["title"]["value"] == "Software Engineer"
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
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))


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


async def test_truncated_at_budget_raises_llm_response_truncated_with_diagnosis():
    """Regression for the 2026-08-27 job-intelligence search failure.

    The content cut mid-string ('...AND recruiting A') and the error showed
    only the first 500 characters — no way to tell a budget truncation from
    plain malformation. When finish_reason=length, the raise is now the
    distinct LLMResponseTruncated with the completion-token spend and the
    tail of the content in the message.
    """
    long_content = '{"platform": "LinkedIn", "queries": ["(\\"talent acquisition\\") AND re'
    payload = {
        "choices": [
            {
                "message": {"content": long_content},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 65536},
        "model": "glm-5.3",
    }

    with pytest.raises(LLMResponseTruncated) as excinfo:
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))
    msg = str(excinfo.value)
    assert "finish_reason=length" in msg
    # The tail points at where it actually stopped, mid-string — repr-quoted
    # inside the diagnosis sentence, so match on the content itself.
    assert "AND re" in msg


async def test_malformed_json_on_unbounded_completion_keeps_class_and_carries_tail():
    """Not every broken answer is a truncation.

    finish_reason=stop means the model *chose* to stop there — that is an
    ordinary invalid answer (re-ask / escalate), not a budget problem, so the
    exception class must stay LLMInvalidJSON while still carrying the tail
    diagnosis. The content ends at a `}` (so the shape check does not call it
    truncated) yet is malformed (the array never closes) — a genuinely bad
    answer, not one cut off mid-JSON.
    """
    payload = {
        "choices": [
            {
                "message": {"content": '{"a": [1, 2}'},  # closes on `}` but the array is unclosed
                "finish_reason": "stop",
            }
        ],
        "usage": {},
        "model": "secret://project/APP_ENV/fast",
    }

    with pytest.raises(LLMInvalidJSON) as excinfo:
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))
    assert not isinstance(excinfo.value, LLMResponseTruncated)
    assert "finish_reason=stop" in str(excinfo.value)


async def test_raw_control_character_in_a_value_parses_leniently():
    """GLM pretty-prints, and when it wraps a long value across lines the
    newline lands RAW inside the string — which strict `json.loads` rejects
    ("Invalid control character at: line N column M").

    Production failure this fixes (job intelligence, 2026-09-30, row
    53ec2133): the answer closed cleanly at `}` and reported
    `finish_reason=stop`, so no truncation retry fired, and the recruiter was
    shown the raw JSON head as the failure reason. Every value still comes
    from the model — the lenient parse only tolerates the character, it
    invents nothing (§15).
    """
    payload = {
        "choices": [
            {
                "message": {
                    # A REAL newline inside the string value, not an escape.
                    "content": '{\n  "role": "line one\nline two"\n}',
                    "finish_reason": "stop",
                }
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"role": "line one\nline two"}


async def test_trailing_comma_in_an_object_parses_with_the_comma_dropped():
    """GLM pretty-prints and sometimes leaves a comma after the last field
    before the closing brace, which strict `json.loads` rejects
    ("Expecting property name enclosed in double quotes").

    Production failure this fixes (job intelligence persona stage, 2026-10):
    the answer closed cleanly at `}` with a trailing comma after its last
    field, so `_looks_truncated` correctly said "not truncated" and no retry
    fired — the recruiter was shown the raw parser error ("Expecting property
    name enclosed in double quotes: line 38 column 4"). Deleting the comma
    invents nothing: every value still comes from the model's answer (§15).
    """
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "{\n"
                        '  "likely_backgrounds": [\n'
                        '    "Retail or hospitality frontline work",\n'
                        '    "Customer service or cash handling experience"\n'
                        "  ],\n"
                        '  "communication_style": "Warm and practical",\n'
                        "}"
                    ),
                    "finish_reason": "stop",
                }
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {
        "likely_backgrounds": [
            "Retail or hospitality frontline work",
            "Customer service or cash handling experience",
        ],
        "communication_style": "Warm and practical",
    }


async def test_trailing_comma_in_an_array_parses_leniently():
    """The same formatting rule in an unseen shape: a trailing comma before a
    nested `]`. The strip is closer-agnostic — `}` and `]` alike — so the
    general rule is what is tested, not the one reported string.
    """
    payload = {
        "choices": [
            {
                "message": {"content": '{"behaviours": ["punctual", "reliable",]}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"behaviours": ["punctual", "reliable"]}


async def test_comma_inside_a_string_value_is_never_touched():
    """The repair is string-aware: a comma inside a value is DATA, not syntax.
    This answer has no trailing comma at all, so the strip must leave it
    untouched — deleting the comma would be fabrication (§15).
    """
    payload = {
        "choices": [
            {
                "message": {"content": '{"note": "yes, } and ] too", "n": 1}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"note": "yes, } and ] too", "n": 1}


async def test_doubled_trailing_commas_parse_after_the_fixpoint_strip():
    """A single strip pass removes only the comma immediately before a closer,
    so two in a row (`reliable",,`) survive it. The bounded fixpoint loop
    strips again and the answer parses — same no-invention argument: a comma
    carries no value, so deleting it adds, defaults, and alters nothing.

    This is an unseen input the general rule covers beyond the reported single
    trailing comma: doubled commas before either a `]` or a `}`.
    """
    payload = {
        "choices": [
            {
                "message": {"content": '{"behaviours": ["punctual", "reliable",,], "n": 1,}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"behaviours": ["punctual", "reliable"], "n": 1}


async def test_json_shaped_garbage_raises_llm_syntax_error():
    """Content that starts with `{`/`[` but cannot be repaired by any
    syntax-noise fix is structural garbage, not prose — it gets the
    `LLMSyntaxError` subclass so the engine re-asks it once. It is still an
    `LLMInvalidJSON` (subclass), so every existing handler catches it, and the
    message still carries the strict parser's own diagnosis.
    """
    payload = {
        "choices": [
            {
                "message": {"content": '{"broken":}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    with pytest.raises(LLMSyntaxError) as excinfo:
        await complete_json(
            "prompt", model="test/fast", schema={}, transport=_transport(payload)
        )
    assert isinstance(excinfo.value, LLMInvalidJSON)
    assert "Expecting" in str(excinfo.value)


async def test_prose_answer_stays_plain_invalid_json():
    """A prose refusal is a meaningful answer whose text IS the diagnosis, so
    it must NOT become an `LLMSyntaxError` — the engine must not retry it away.
    """
    payload = {
        "choices": [
            {
                "message": {"content": "The job order states almost nothing to analyse."},
                "finish_reason": "stop",
            }
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    with pytest.raises(LLMInvalidJSON) as excinfo:
        await complete_json(
            "prompt", model="test/fast", schema={}, transport=_transport(payload)
        )
    assert not isinstance(excinfo.value, LLMSyntaxError)


async def test_genuinely_broken_json_reports_the_parser_diagnosis():
    """Leniency must not paper over real malformation. An unescaped inner
    quote fails both the strict and the lenient parse, so this stays
    `LLMInvalidJSON` — and the message now carries the strict parser's own
    reason, which says WHERE the answer broke. The old code raised the head
    slice alone, so an answer broken past the first 500 characters left no
    offset to work from.
    """
    content = '{"role": "say "hi" now"}'
    payload = {
        "choices": [
            {"message": {"content": content}, "finish_reason": "stop"}
        ],
        "usage": {},
        "model": "glm-4.6",
    }

    with pytest.raises(LLMInvalidJSON) as excinfo:
        await complete_json(
            "prompt", model="test/fast", schema={}, transport=_transport(payload)
        )

    message = str(excinfo.value)
    assert "Expecting" in message or "Invalid" in message
    assert content[:500] in message
    assert "finish_reason=stop" in message


async def test_missing_finish_reason_keeps_class_unknown_but_not_truncated():
    """A provider that omits finish_reason must not be mistaken for one
    that said length — the remedy differs (grow budget vs re-ask).

    The content ends at `}` (complete-looking) so this is a plain invalid
    answer; only the mid-JSON shape promotes to truncated here, not the
    absence of a reason on its own."""
    payload = {
        "choices": [{"message": {"content": '{"broken":}'}}],
        "usage": {},
        "model": "secret://project/APP_ENV/fast",
    }

    with pytest.raises(LLMInvalidJSON) as excinfo:
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))
    assert not isinstance(excinfo.value, LLMResponseTruncated)
    assert "no finish_reason" in str(excinfo.value)


async def test_truncated_mid_key_with_stop_is_classified_truncated():
    """Regression for the Job Intelligence analyses failure this fixes.

    The provider cut the answer mid-key (`...","business_purpos`) while
    reporting finish_reason=stop. Trusting the reported reason alone left it a
    plain LLMInvalidJSON, so the engine's truncation retry never fired and the
    recruiter saw the raw JSON head as the failure reason. The shape check now
    catches it: content that does not end at a JSON closer is truncated, and
    the message names the provider's actual (non-length) reason plus the tail.
    """
    truncated_content = (
        '{"role":"Finance Operations Analyst","business_purpos'
    )
    payload = {
        "choices": [
            {
                "message": {"content": truncated_content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 800, "completion_tokens": 32000},
        "model": "glm-4.6",
    }

    with pytest.raises(LLMResponseTruncated) as excinfo:
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))
    msg = str(excinfo.value)
    assert "finish_reason=stop" in msg
    assert "business_purpos" in msg


async def test_truncated_mid_key_with_no_finish_reason_is_classified_truncated():
    """Same shape check applies when the provider reports no finish_reason."""
    payload = {
        "choices": [{"message": {"content": '{"role":"Finance","business'}}],
        "usage": {},
        "model": "glm-4.6",
    }

    with pytest.raises(LLMResponseTruncated) as excinfo:
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))
    assert "no finish_reason" in str(excinfo.value)


async def test_complete_but_malformed_json_ending_in_brace_stays_invalid():
    """Content that ends at `}` but will not parse is not a budget truncation,
    so the truncation retry must not fire — it stays an `LLMInvalidJSON`.

    The defect used here is a missing delimiter, which no syntax-noise repair
    can fix. (This test previously used a doubled trailing comma; user
    directive 2026-10-10 makes the strip run to a fixpoint, so that case now
    parses — see `test_doubled_trailing_commas_parse_after_the_fixpoint_strip`.)
    """
    payload = {
        "choices": [{"message": {"content": '{"role": "Analyst" "n": 1}'}}],
        "usage": {},
        "model": "glm-4.6",
    }

    with pytest.raises(LLMInvalidJSON) as excinfo:
        await complete_json("prompt", model="test/fast", schema={}, transport=_transport(payload))
    assert not isinstance(excinfo.value, LLMResponseTruncated)

