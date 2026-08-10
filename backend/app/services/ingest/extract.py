"""Prompt construction and the extraction call (plan §12, §13, §32).

The prompt asks for a quotation and for `Not mentioned`, and neither is
trusted: the schema rejects a value that quotes nothing, and evidence.py looks
for the quotation in the email. The prompt only makes compliance likely; the
code makes non-compliance visible.

Runs on DeepSeek, like the gate, and asks for a plain JSON object. Both of
those are scars. Extraction was routed through OpenRouter with a strict
`json_schema` response format, and the escalation model rejected the schema —
"the compiled grammar is too large" — for every email, so production held four
recruitment emails stuck at `extracting` and zero opportunities. The schema is
now prompt text, which no provider compiles and none can refuse.

Two passes, one prompt, one model. The first is cheap and handles everything;
the second is the same model thinking harder, reached for only when the first
answer fails a check we can make ourselves — never on a hunch, and never as a
plain retry of something that already worked.
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
- A headcount is not a second vacancy. "Operations Assistant x 2", "2 positions",
  "x2" or a similar multiplier means one role with multiple openings — emit ONE
  entry for it, never one per opening. Two entries are only correct when the
  roles themselves differ (different title, different company, or a different
  location/schedule that makes them genuinely separate posts).
- For each field, `evidence` must be text copied VERBATIM from the EMAIL below —
  character for character, with nothing added, shortened or paraphrased. This is
  checked against the email; a quote that is not in it is discarded.
- `start_char` and `end_char` are your best estimate of where that quote starts
  and ends in the EMAIL. They are only used to tell apart two identical quotes,
  so approximate is fine — never change the quote to fit them.
- If the email does not state a field, set its value to "{not_mentioned}" and
  omit the offsets. Never infer, estimate, or fill in a typical value.
- `salary_period` is one of: hour, day, month, year. Extract it separately from
  the amount.
- `salary_min` and `salary_max` are the lowest and highest MONTHLY GROSS figures
  the client would pay for this role, as plain numbers in `value` (e.g. "2700"
  or "5300"). When the salary is compound — basic plus an allowance, bonus or
  incentive ("$4500 basic max + $800 rotating shift allowance") — `salary_max`
  is the total the client pays (5300), and `value` may equal the sum of figures
  the quoted `evidence` states. Set `salary_min` to the lowest stated figure
  and `salary_max` to the highest. If only one figure is stated, put it in both.
  If the email states no salary at all, set both to "{not_mentioned}".
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


def _attempts() -> tuple[tuple[str, str], ...]:
    """The (model, reasoning effort) pairs to try, cheapest first.

    `EXTRACTION_MODEL_STRONG` falls back to the fast model rather than being
    required, so a deployment that names only one model still escalates — by
    effort — instead of sending an empty model id the provider rejects. That is
    the failure mode this whole change exists to remove: a second attempt whose
    request shape was never verified is worse than no second attempt.
    """
    fast = settings.EXTRACTION_MODEL_FAST
    return (
        (fast, settings.EXTRACTION_REASONING_EFFORT_FAST),
        (
            settings.EXTRACTION_MODEL_STRONG or fast,
            settings.EXTRACTION_REASONING_EFFORT_STRONG,
        ),
    )


async def extract(source: str, *, llm=None) -> tuple[ExtractionResponse, LLMResult]:
    """Extract, escalating only when the first pass demonstrably fell short.

    Escalation is not a retry of the same thing (§32). Temperature is zero, so
    re-asking the same model the same question buys a second bill and the same
    answer — which is precisely why the second attempt raises `reasoning_effort`
    rather than repeating the request. What justifies it is evidence that the
    first pass could not do *this* email: either it returned something
    unparseable, or the text it quoted is not in the email. Both are decided
    here by code, never by the model's opinion of its own work.

    `schema=None` is deliberate and is the fix for the production outage: it
    asks for a bare `json_object`. The schema itself travels in the prompt, so
    nothing has to compile a fourteen-field grammar, and there is no request here
    that a provider can reject on shape.

    `llm` defaults to None rather than to `complete_json` for the reason
    classify.py gives: a default argument binds the function object at
    definition time, and monkeypatching this module would then do nothing.
    """
    resolve = llm or complete_json
    prompt = build_prompt(source)
    last: tuple[ExtractionResponse, LLMResult] | None = None
    failure: Exception | None = None

    for model, effort in _attempts():
        try:
            result = await resolve(
                prompt,
                model=model,
                schema=None,
                base_url=settings.DEEPSEEK_BASE_URL,
                api_key=settings.DEEPSEEK_API_KEY,
                extra_body={
                    "max_tokens": settings.EXTRACTION_MAX_TOKENS,
                    "reasoning_effort": effort,
                },
            )
            response = ExtractionResponse.model_validate(result.data)
        except (LLMInvalidJSON, ValueError) as exc:
            # A ValueError here is the schema refusing a value that quotes
            # nothing — indistinguishable, for routing purposes, from JSON we
            # could not parse: this model did not answer in the required shape.
            failure = exc
            log.warning(
                "extraction_unusable", model=model, effort=effort, error=repr(exc)
            )
            continue

        last = (response, result)
        if not _needs_a_better_model(response, source):
            return last
        log.info(
            "extraction_escalating", model=model, effort=effort, jobs=len(response.jobs)
        )

    if last is not None:
        # Both passes were made and neither produced quotations that all check
        # out. The later answer is returned rather than discarded: persist
        # records the failed checks as `needs_review`, which is a vacancy a
        # recruiter can still see and correct. Raising instead would throw away
        # a usable extraction because it was imperfect.
        return last

    raise LLMInvalidJSON(
        f"neither extraction pass returned a valid answer: {failure}"
    ) from failure


def _needs_a_better_model(response: ExtractionResponse, source: str) -> bool:
    """A run with no vacancy in it is not a failure.

    Most email genuinely contains no job order, and the gate fails open by
    design, so plenty of what reaches here is correctly empty. Escalating on
    emptiness would send every one of those to the expensive model to be told
    the same thing again.
    """
    return any(quality_state(job, source) == _ESCALATE_FROM for job in response.jobs)
