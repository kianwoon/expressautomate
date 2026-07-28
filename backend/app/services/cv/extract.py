"""Reading a career off a CV, and proving each line came from the page (§15).

The shape is the one email extraction uses — two passes, spans verified against
the source, a stronger attempt only on proof that the first fell short — but
none of its code: that pipeline validates against the *vacancy* schema and
wraps its prompt around an email body. What carries over is the discipline.

One thing is deliberately stricter here. When a vacancy fails its checks the
email is kept beside it and a recruiter sees it flagged, so a doubtful
extraction is still worth showing. A doubtful employment history is not: it
reads as a candidate's career, gets shortlisted on, and is quoted to a client.
So a role whose quotations are not on the page is dropped after the second
attempt rather than published under a caveat.

`_attempts()` is imported rather than re-derived. It is pure configuration —
the fast model at low effort, then the strong model at high effort, with the
fallback that keeps a single-model deployment escalating by effort instead of
sending an empty model id — and a second copy would drift from it.
"""

import json

from app.core.config import settings
from app.core.logging import get_logger
from app.services.cv.schema import CVResponse, ExtractedRole, cv_json_schema
from app.services.ingest.evidence import verify
from app.services.ingest.extract import _attempts
from app.services.ingest.schema import NOT_MENTIONED
from app.services.llm.client import LLMInvalidJSON, LLMResult, complete_json

log = get_logger(__name__)

# allow-hardcode: a prompt, not configuration.
PROMPT = """Extract this candidate's employment history and skills from the CV.

Rules:
- One entry in `roles` per distinct position held. List them as the CV does.
- For each field, `evidence` must be text copied VERBATIM from the CV below —
  character for character, with nothing added, shortened or paraphrased. This
  is checked against the CV; a role whose quote is not in it is discarded
  entirely, so quoting loosely loses the whole position.
- `start_char` and `end_char` are your best estimate of where that quote starts
  and ends in the CV. They are only used to tell apart two identical quotes, so
  approximate is fine — never change the quote to fit them.
- For `start_date` and `end_date`, `precision` states how exactly the CV wrote
  the date, and it must match what is actually on the page: "2019" is `year`,
  "Mar 2019" is `month`, "3 March 2019" is `day`. Never add a day or a month
  the CV did not print, and never complete a partial date — a first of the
  month that the candidate never wrote is a fabrication, not a formatting
  choice. Copy the date into `value` as the CV gives it.
- A role still in progress has no end date: set `end_date` to "{not_mentioned}".
- If the CV does not state a field, set its value to "{not_mentioned}" and omit
  the offsets. Never infer, estimate, or fill in a typical value — not a title
  from a company, not a company from an email address, not a skill from a job
  title. If you are unsure, "{not_mentioned}" is the correct answer.
- `skills` are only the ones the CV names. Do not expand an abbreviation into a
  skill the page does not contain.

Return JSON matching this schema:
{schema}

CV:
{cv}
"""


def build_prompt(source: str) -> str:
    """Separate from `extract_cv` so a prompt change is testable without a model."""
    return PROMPT.format(
        not_mentioned=NOT_MENTIONED,
        schema=json.dumps(cv_json_schema()),
        cv=source,
    )


async def extract_cv(text: str, *, llm=None) -> tuple[CVResponse, LLMResult]:
    """Extract a career, escalating only when the first pass demonstrably failed.

    Escalation is not a retry. Temperature is zero, so re-asking the same model
    the same question buys a second bill and the same answer — which is why the
    second attempt raises `reasoning_effort`. What justifies it is evidence that
    the first pass could not do *this* CV: either it answered in a shape we
    cannot parse (which includes claiming a date precision the page does not
    support), or the text it quoted is not on the page. Both are decided here by
    code, never by the model's opinion of its own work.

    `schema=None` is deliberate: it asks for a bare `json_object`. The schema
    travels in the prompt, so no provider has to compile a grammar it may
    refuse — the outage that taught email extraction the same lesson.

    `llm` defaults to None rather than to `complete_json` because a default
    argument binds the function object at definition time, and monkeypatching
    this module would then do nothing.
    """
    resolve = llm or complete_json
    prompt = build_prompt(text)
    last: tuple[CVResponse, LLMResult] | None = None
    failure: Exception | None = None

    for model, effort in _attempts():
        try:
            result = await resolve(
                prompt,
                model=model,
                schema=None,
                base_url=settings.CEREBRAS_BASE_URL,
                api_key=settings.CEREBRAS_API_KEY,
                extra_body={
                    "max_tokens": settings.EXTRACTION_MAX_TOKENS,
                    "reasoning_effort": effort,
                },
            )
            response = CVResponse.model_validate(result.data)
            response = _drop_unstated(response)
        except (LLMInvalidJSON, ValueError) as exc:
            # A ValueError here is the schema refusing a value that quotes
            # nothing, or a date claiming more precision than the page carries.
            # For routing purposes both are the same fact as unparseable JSON:
            # this model did not answer in the required shape.
            failure = exc
            log.warning("cv_extraction_unusable", model=model, error=repr(exc))
            continue

        last = (response, result)
        if not _needs_a_better_model(response, text):
            return last
        log.info("cv_extraction_escalating", model=model, roles=len(response.roles))

    if last is not None:
        # Both passes were made and neither quoted the page throughout. What is
        # returned is the later answer with the unsupported parts removed, so a
        # CV that was two-thirds read still yields those two-thirds. Returning
        # it whole would publish a career the page does not contain; raising
        # would throw away the part of it that is real.
        response, result = last
        return _only_what_the_page_supports(response, text), result

    raise LLMInvalidJSON(
        f"neither CV extraction pass returned a valid answer: {failure}"
    ) from failure


def _fields(role: ExtractedRole) -> list:
    """The stated fields of a role. A "Not mentioned" one has nothing to check."""
    return [f for f in vars(role).values() if f is not None and not f.is_missing]


def _drop_unstated(response: CVResponse) -> CVResponse:
    """Discard rows that assert nothing before verification ever sees them.

    A role every field of which is `None` or "Not mentioned" has no quotation
    to check, so `_role_is_supported`'s `all(...)` over an empty list would
    vacuously say yes — that is a bug in a support check, not license to publish
    an empty row as a position held. Likewise a skill whose value is the
    not-mentioned sentinel has no claim in it for `verify` to test, and passes
    for the same vacuous reason. Neither is a fabrication by itself, but both
    are noise that would otherwise ride along as if the CV had said something.
    """
    return CVResponse(
        roles=[r for r in response.roles if _fields(r)],
        skills=[s for s in response.skills if not s.is_missing],
    )


def _role_is_supported(role: ExtractedRole, source: str) -> bool:
    """Every quotation in the role must be findable in the CV.

    All or nothing, per role. A position whose title checks out but whose dates
    quote nothing is not a partial success — it is a row in an employment
    history with a made-up span of time in it, which is exactly the artefact a
    recruiter would read as fact.

    `verify` also writes the located offsets back over the model's arithmetic,
    so the offsets that survive this call point at real characters.
    """
    return all(verify(field, source) for field in _fields(role))


def _needs_a_better_model(response: CVResponse, source: str) -> bool:
    """Anything unsupported is worth one more, more careful attempt.

    An empty result is not escalated. A CV that yielded no role is usually a
    document that is not a CV at all — a covering letter, a scanned page with
    no text layer — and asking a more expensive model to confirm that costs
    money to be told the same thing twice. Task 5 is where an empty extraction
    becomes a visible state rather than a silence.
    """
    unsupported_role = any(
        not _role_is_supported(role, source) for role in response.roles
    )
    unsupported_skill = any(not verify(s, source) for s in response.skills)
    return unsupported_role or unsupported_skill


def _only_what_the_page_supports(response: CVResponse, source: str) -> CVResponse:
    """Drop what neither pass could show on the page.

    This runs only after both attempts, so nothing is discarded that a second
    opinion might have rescued. `verify` has already been called on all of it by
    `_needs_a_better_model` — the offsets are corrected and the answers are the
    same — so calling it again here costs a string search and keeps this
    function honest on its own terms rather than dependent on call order.
    """
    return CVResponse(
        roles=[r for r in response.roles if _role_is_supported(r, source)],
        skills=[s for s in response.skills if verify(s, source)],
    )
