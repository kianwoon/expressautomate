"""Prompt construction and the extraction call (plan §12, §13, §32).

The prompt insists on offsets and on `Not mentioned`, but neither is trusted:
the schema rejects a value with no offsets, and evidence.py checks the offsets
against the source. The prompt only makes compliance likely; the code makes
non-compliance visible.

Two models, one prompt. The fast model handles everything and the strong one is
reached for only when the fast answer fails a check we can make ourselves —
never on a hunch, and never as a plain retry of something that already worked.
"""

import json

from app.core.config import settings
from app.core.logging import get_logger
from app.services.ingest.evidence import quality_state
from app.services.ingest.schema import NOT_MENTIONED, ExtractionResponse, json_schema
from app.services.llm.client import LLMInvalidJSON, LLMResult, complete_json

log = get_logger(__name__)

# allow-hardcode: a prompt, not configuration.
PROMPT = """Extract every job vacancy described in this email.

Rules:
- One entry in `jobs` per distinct vacancy. An email may describe several, or none.
- For each field, quote the exact text you took it from in `evidence`, and give
  `start_char` and `end_char` — the character offsets of that quote in the EMAIL
  text below. The quote must match the email exactly at those offsets.
- If the email does not state a field, set its value to "{not_mentioned}" and
  omit the offsets. Never infer, estimate, or fill in a typical value.
- `salary_period` is one of: hour, day, month, year. Extract it separately from
  the amount.
- `work_arrangement` is one of: onsite, hybrid, remote.

Return JSON matching this schema:
{schema}

EMAIL:
{email}
"""

# The one state that means a deterministic check failed: a span that is not in
# the source, a salary with no number in it, or a confidence below the bar. It
# is the only signal worth a second, more expensive call — `likely` is a real
# answer with a caveat, and paying twice for it would double the bill of the
# ordinary case.
_ESCALATE_FROM = "needs_review"


def build_prompt(source: str) -> str:
    """Separate from `extract` so a prompt change is testable without a model."""
    return PROMPT.format(
        not_mentioned=NOT_MENTIONED,
        schema=json.dumps(json_schema()),
        email=source,
    )


async def extract(source: str, *, llm=None) -> tuple[ExtractionResponse, LLMResult]:
    """Extract, escalating to the strong model only when the fast one fell short.

    Escalation is not a retry of the same thing (§32). Temperature is zero, so
    re-asking the same model the same question buys a second bill and the same
    answer. What justifies a second call is evidence that *this* model could
    not do *this* email: either it returned something unparseable, or the spans
    it named are not in the source. Both are decided here by code, never by the
    model's opinion of its own work.

    `llm` defaults to None rather than to `complete_json` for the reason
    classify.py gives: a default argument binds the function object at
    definition time, and monkeypatching this module would then do nothing.
    """
    resolve = llm or complete_json
    prompt = build_prompt(source)
    models = (settings.EXTRACTION_MODEL_FAST, settings.EXTRACTION_MODEL_STRONG)
    last: tuple[ExtractionResponse, LLMResult] | None = None
    failure: Exception | None = None

    for model in models:
        try:
            result = await resolve(prompt, model=model, schema=json_schema())
            response = ExtractionResponse.model_validate(result.data)
        except (LLMInvalidJSON, ValueError) as exc:
            # A ValueError here is the schema refusing a value that quotes
            # nothing — indistinguishable, for routing purposes, from JSON we
            # could not parse: this model did not answer in the required shape.
            failure = exc
            log.warning("extraction_unusable", model=model, error=repr(exc))
            continue

        last = (response, result)
        if not _needs_a_better_model(response, source):
            return last
        log.info("extraction_escalating", model=model, jobs=len(response.jobs))

    if last is not None:
        # Both models were reached and neither produced spans that all check
        # out. The later answer is returned rather than discarded: persist
        # records the failed checks as `needs_review`, which is a vacancy a
        # recruiter can still see and correct. Raising instead would throw away
        # a usable extraction because it was imperfect.
        return last

    raise LLMInvalidJSON(
        f"neither model returned a valid extraction: {failure}"
    ) from failure


def _needs_a_better_model(response: ExtractionResponse, source: str) -> bool:
    """A run with no vacancy in it is not a failure.

    Most email genuinely contains no job order, and the gate fails open by
    design, so plenty of what reaches here is correctly empty. Escalating on
    emptiness would send every one of those to the expensive model to be told
    the same thing again.
    """
    return any(quality_state(job, source) == _ESCALATE_FROM for job in response.jobs)
