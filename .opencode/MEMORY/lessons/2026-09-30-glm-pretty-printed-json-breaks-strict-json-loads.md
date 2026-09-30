---
title: GLM pretty-printed answers break strict json.loads on raw control characters
date: 2026-09-30
type: lesson
tags: [llm, json, glm, parsing, job-intelligence]
---

# GLM pretty-printed JSON breaks strict `json.loads`

## Pattern

GLM coding-plan answers repeatedly break strict `json.loads`. This is the 6th+
fix in the same family, the earlier ones being envelope unwraps, the
`_rescue_answer_envelope` rescue, and list coercion. When GLM pretty-prints an
answer and wraps a long value across lines, the newline lands RAW inside the
string. `json.loads` defaults to `strict=True`, which rejects a raw control
character inside a string value ("Invalid control character at: line N column
M").

## Symptom

`LLMInvalidJSON` on an answer that reported `finish_reason=stop` and closed
cleanly at `}` — so the truncation retry never fired and the wrong class was
assigned. The failure message was the first 500 chars of the answer alone,
which showed a well-formed head and a valid tail while hiding the defect in
the elided middle. Diagnosing it took a round trip through the arq service
logs that returned nothing useful, because the summarizer had already
discarded the detail at write time.

## Fix

`_loads` in `backend/app/services/llm/client.py`: attempt the strict parse
first, fall back once to `strict=False`, and when both fail raise with the
STRICT parser's own diagnosis ahead of the head slice. Every value still
comes from the model — the lenient pass only tolerates the character, it
invents nothing (§15). `_parse` and its envelope-string branch both go
through `_loads`.

The generalisable part: **an error message that summarises the payload makes
the failure undiagnosable.** Leading with the parser's line/column cost
nothing and is the one detail that says where the answer broke.

## Acceptance gate

The two new tests in `backend/tests/test_llm_client.py`:

- `test_raw_control_character_in_a_value_parses_leniently` — a real newline
  inside a string value parses via the lenient path.
- `test_genuinely_broken_json_reports_the_parser_diagnosis` — an unescaped
  inner quote still raises `LLMInvalidJSON`, and the message carries both the
  strict parser's reason and the head slice.

## Related

`test_malformed_json_on_unbounded_completion_keeps_class_and_carries_tail` and
`test_missing_finish_reason_keeps_class_unknown_but_not_truncated` guard that
the classification and the `_truncate_reason` note are unchanged by the lenient
path. Both assertions in those tests must stay.
