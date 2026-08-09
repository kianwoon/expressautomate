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
import re

from app.core.config import settings
from app.core.logging import get_logger
from app.services.cv.schema import CVResponse, ExtractedRole, ExtractedSalary, cv_json_schema
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


# allow-hardcode: a prompt, not configuration.
SALARY_PROMPT = """Read the salary figures the candidate stated on this CV.

Rules:
- `last_drawn_salary`: the salary the candidate last or currently earns.
- `expected_salary`: the salary the candidate is asking for next.
- For each, return the structured parts:
  - `amount`: the numeric amount in ONE period (the figure as stated, not annualised).
    "$ 5000 x 12" means 5000/month (the x12 is months per year); "$60,000/year"
    means 60000/year.
  - `currency`: the ISO code. A bare "$" on a Singapore CV is SGD; "RM" is MYR.
    Use the currency the CV states, or SGD if it only prints "$".
  - `period`: one of "hour", "day", "week", "month", or "year".
  - `evidence`: the EXACT text from the CV stating this salary, copied VERBATIM.
    This is checked — a salary whose quote is not on the page is discarded.
  - If the CV states no salary, set `amount` to null and all other fields to null.
- A "current" figure is not an "expected" one — never move one into the other.

Return JSON with this shape:
{{"last_drawn_salary": {{"amount": number|null, "currency": string|null,
  "period": string|null, "evidence": string|null, "confidence": number}},
"expected_salary": {{"amount": number|null, "currency": string|null,
  "period": string|null, "evidence": string|null, "confidence": number}}}}

CV:
{cv}
"""


def build_prompt(source: str) -> str:
    """Separate from `extract_cv` so a prompt change is testable without a model."""
    # The roles/skills schema excludes salary — salary is extracted in a
    # separate call from a smaller text window (see `_salary_window`).
    schema = cv_json_schema()
    roles_schema = {
        "type": "object",
        "properties": {
            "roles": schema["properties"]["roles"],
            "skills": schema["properties"]["skills"],
        },
        "required": ["roles", "skills"],
        "additionalProperties": False,
    }
    return PROMPT.format(
        not_mentioned=NOT_MENTIONED,
        schema=json.dumps(roles_schema),
        cv=source,
    )


def build_salary_prompt(source: str) -> str:
    """The salary-only prompt, testable without a model."""
    return SALARY_PROMPT.format(cv=source)


# Salary lives in the personal-particulars header, always near the top of a CV.
# A window this size captures it without feeding the model the entire career
# history — which is what made the model give up on large CVs.
_SALARY_WINDOW_CHARS = 2000


def _salary_window(text: str) -> str:
    """The slice of a CV where salary is stated: the personal-particulars header.

    Salary ("Last Drawn", "Expected Salary") is always in the header section at
    the top of a CV, never buried in employment history. Sending just this window
    to the model means salary extraction succeeds even on a CV large enough that
    the full-document career extraction bails — the two are independent calls.
    """
    return text[:_SALARY_WINDOW_CHARS]


async def extract_cv(text: str, *, llm=None) -> tuple[CVResponse, LLMResult]:
    """Extract a career and salary, in two independent calls.

    Roles and skills are read from the full document with the two-pass
    escalation (fast model → strong model when evidence fails). Salary is read
    from a small window at the top of the CV (the personal-particulars header
    where "Last Drawn" / "Expected Salary" always sit), so a CV large enough
    that the career extraction bails still yields its salary — the two calls
    are independent, and a failure in one does not lose the other.

    `schema=None` is deliberate: it asks for a bare `json_object`. The schema
    travels in the prompt, so no provider has to compile a grammar it may refuse
    — the outage that taught email extraction the same lesson.

    `llm` defaults to None rather than to `complete_json` because a default
    argument binds the function object at definition time, and monkeypatching
    this module would then do nothing.
    """
    resolve = llm or complete_json

    # Salary: small window, one pass. Fast and reliable — never the bottleneck.
    salary_response = await _extract_salary(text, resolve)

    # Roles + skills: full document, two-pass escalation.
    career_response, result = await _extract_career(text, resolve)

    # Merge: salary fills into the career response.
    return (
        CVResponse(
            roles=career_response.roles,
            skills=career_response.skills,
            last_drawn_salary=salary_response.last_drawn_salary,
            expected_salary=salary_response.expected_salary,
        ),
        result,
    )


async def _extract_salary(
    text: str, resolve
) -> CVResponse:
    """Extract salary from the personal-particulars window. Never raises.

    A salary extraction failure (model error, parse error) leaves both fields
    None — honest absence, not a crashed pipeline. The career extraction is
    independent and still runs. Evidence is verified against the full CV text,
    not just the window, so a fabricated quote is caught regardless.
    """
    window = _salary_window(text)
    try:
        result = await resolve(
            build_salary_prompt(window),
            model=settings.EXTRACTION_MODEL_FAST,
            schema=None,
            base_url=settings.DEEPSEEK_BASE_URL,
            api_key=settings.DEEPSEEK_API_KEY,
            extra_body={
                "max_tokens": settings.EXTRACTION_MAX_TOKENS,
            },
        )
        data = result.data
        ld = data.get("last_drawn_salary")
        ex = data.get("expected_salary")
        response = CVResponse(
            last_drawn_salary=_verified_salary(
                ExtractedSalary.model_validate(ld) if ld else None, text
            ),
            expected_salary=_verified_salary(
                ExtractedSalary.model_validate(ex) if ex else None, text
            ),
        )
        return response
    except (LLMInvalidJSON, ValueError, TypeError) as exc:
        log.warning("cv_salary_extraction_failed", error=repr(exc))
        return CVResponse()


async def _extract_career(text: str, resolve) -> tuple[CVResponse, LLMResult]:
    """Extract roles and skills from the full document, escalating on failure.

    Escalation is not a retry. Temperature is zero, so re-asking the same model
    the same question buys a second bill and the same answer — which is why the
    second attempt raises `reasoning_effort`. What justifies it is evidence that
    the first pass could not do *this* CV: either it answered in a shape we
    cannot parse (which includes claiming a date precision the page does not
    support), or the text it quoted is not on the page. Both are decided here by
    code, never by the model's opinion of its own work.
    """
    prompt = build_prompt(text)
    last: tuple[CVResponse, LLMResult] | None = None
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


def _verified_salary(
    field: ExtractedSalary | None, source: str
) -> ExtractedSalary | None:
    """Keep a salary only if its evidence quote is on the page, else drop to None.

    The model returns structured parts (amount/currency/period) plus an
    `evidence` string copied from the CV. That evidence is the anti-fabrication
    check — if it is not found verbatim on the page, the amount is not trusted.
    """
    if field is None or field.is_missing:
        return None
    evidence = (field.evidence or "").strip()
    if not evidence or _normalise(evidence) not in _normalise(source):
        return None
    return field


def _normalise(text: str) -> str:
    """Whitespace-insensitive contains check, matching `ingest.evidence.verify`."""
    return re.sub(r"\s+", " ", text).strip().lower()


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
