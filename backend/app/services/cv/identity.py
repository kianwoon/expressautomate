"""Reading a person's contact details off a CV so it can create its own candidate.

This is the one piece the existing CV pipeline deliberately does not produce.
`extract_cv` reads a career — roles and skills — and, by design, nothing about
the person: the schema in `cv/schema.py` refuses to carry sex, date of birth,
nationality or race, and the same privacy line is held here. What this module
adds is only the three facts needed to decide *who* a CV belongs to and whether
they are already in the database: a name to display, and an email or phone to
match on. Identity is email-or-phone (`candidate_matching.find_candidate`); a
name is never a key, only a label.

The discipline is the one `extract_cv` and the vacancy extractor already keep:
the model quotes the page, and nothing is trusted until that quotation is found
in the source. `IdentityField` inherits `ExtractedField` for the same reason
`ExtractedDate` does — the validator on it *is* the no-fabrication rule, and a
second copy is a second place for it to be weakened. `ingest.evidence.verify`
then locates each quotation in the CV, and a value whose quote is not on the
page is dropped to absence rather than published as a fact the candidate never
wrote.

Runs on Cerebras in the worker, like every other extraction: the api process
holds no LLM credentials, and an identity call made there would fall back to an
empty base URL and 400.
"""

import json

from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger
from app.services.cv.schema import ExtractedField
from app.services.ingest.evidence import verify
from app.services.ingest.schema import NOT_MENTIONED
from app.services.llm.client import LLMResult, complete_json

log = get_logger(__name__)

# allow-hardcode: a prompt, not configuration.
PROMPT = """Read this CV and find the candidate's contact details only.

Rules:
- Extract only `full_name`, `email`, and `phone`. Nothing else about the person
  is wanted — never sex, date of birth, nationality, or race, even if the CV
  states them.
- For each field, `evidence` must be text copied VERBATIM from the CV below —
  character for character, with nothing added, shortened or paraphrased. This is
  checked against the CV; a value whose quote is not in it is discarded, so
  quoting loosely loses the fact.
- `start_char` and `end_char` are your best estimate of where that quote starts
  and ends in the CV. They are only used to tell apart two identical quotes, so
  approximate is fine — never change the quote to fit them.
- `full_name` is the person's own name as the CV prints it, not a company, a
  referee, or a reviewer named in a margin.
- `email` is the candidate's own email address as written on the page.
- `phone` is the candidate's own phone number as written on the page.
- If the CV does not state a field, set its value to "{not_mentioned}" and omit
  the offsets. Never infer, estimate, or fill in a typical value — not an email
  from a domain, not a country code the number omits, not a name from a header.
  If you are unsure, "{not_mentioned}" is the correct answer.

Return JSON matching this schema:
{schema}

CV:
{cv}
"""


class IdentityField(ExtractedField):
    """A contact field that carries its own proof.

    Inherits `ExtractedField` unchanged, so the validator that refuses a present
    value with no quotation is the same one the vacancy and career extractors
    enforce. A separate name keeps the intent legible at the call site; a second
    implementation of the rule would be the second place it could be weakened.
    """


class IdentityResult(BaseModel):
    """A name to show, and an email or phone to match on — and nothing else.

    Every field is optional because a real CV can omit any of them, and the
    caller's contract is that absence is honest: a None here means the page did
    not state the fact (or stated it in a quotation that did not check out),
    never that the model forgot to look. A result with no email and no phone is
    the signal that the document did not identify a person, and the caller
    refuses to invent one from a name alone.
    """

    full_name: IdentityField | None = None
    email: IdentityField | None = None
    phone: IdentityField | None = None

    @property
    def has_matchable_identity(self) -> bool:
        """Whether this result carries a key `find_candidate` can resolve on.

        A name alone is not an identity — matching on names is exactly the merge
        hazard `candidate_matching` refuses to allow. The caller uses this to
        decide between creating a candidate and holding the CV for review.
        """
        return self.email is not None or self.phone is not None


def identity_json_schema() -> dict:
    """Schema sent to the model. Derived, so it cannot drift from the parser.

    Sent as text inside the prompt rather than as a `json_schema` response
    format, for the reason `cv_json_schema` records: a provider that compiled
    the schema refused the document outright. Written to satisfy strict
    structured output all the same — `additionalProperties: false` and every
    property `required` — so it stays usable as a real response format where a
    provider can compile it.
    """
    field_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "evidence": {"type": ["string", "null"]},
            "start_char": {"type": ["integer", "null"]},
            "end_char": {"type": ["integer", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["value", "evidence", "start_char", "end_char", "confidence"],
        "additionalProperties": False,
    }
    nullable = {**field_schema, "type": ["object", "null"]}
    return {
        "type": "object",
        "properties": {
            "full_name": nullable,
            "email": nullable,
            "phone": nullable,
        },
        "required": ["full_name", "email", "phone"],
        "additionalProperties": False,
    }


def build_prompt(source: str) -> str:
    """Separate from `extract_identity` so a prompt change is testable without a model."""
    return PROMPT.format(
        not_mentioned=NOT_MENTIONED,
        schema=json.dumps(identity_json_schema()),
        cv=source,
    )


def _verified(field: IdentityField | None, source: str) -> IdentityField | None:
    """The field only if its quotation is actually on the page, else None.

    A value the page does not support is a fabrication, and `verify` is the
    mechanical check for it — the same check the career and vacancy extractors
    depend on. Dropping to None rather than raising keeps an identity result
    usable when one field is sound and another is not: an honest email with a
    misquoted name still resolves the candidate, and a missing name is a display
    problem, not a matching one.
    """
    if field is None or field.is_missing:
        return None
    return field if verify(field, source) else None


def _drop_unverified(response: IdentityResult, source: str) -> IdentityResult:
    """Keep only the fields whose quotations the CV actually contains."""
    return IdentityResult(
        full_name=_verified(response.full_name, source),
        email=_verified(response.email, source),
        phone=_verified(response.phone, source),
    )


async def extract_identity(text: str, *, llm=None) -> tuple[IdentityResult, LLMResult]:
    """Read a CV once and return the contact details it honestly states.

    One pass, one model. Identity extraction is three fields, not a career of
    roles each carrying dates at a precision the page must support, so the
    two-pass escalation that `extract_cv` needs has no analogue here: re-asking
    the same model the same question at temperature zero buys a second bill and
    the same answer. A model or prompt fixed tomorrow makes the same file work,
    so an unusable answer propagates as `LLMInvalidJSON` and the caller marks the
    document `failed` rather than silently producing nobody.

    `schema=None` is deliberate: it asks for a bare `json_object`. The schema
    travels in the prompt, so no provider has to compile a grammar it may refuse
    — the outage that taught the same lesson to email and CV extraction.

    `llm` defaults to None rather than to `complete_json` because a default
    argument binds the function object at definition time, and monkeypatching
    this module would then do nothing.
    """
    resolve = llm or complete_json
    result = await resolve(
        build_prompt(text),
        model=settings.EXTRACTION_MODEL_FAST,
        schema=None,
        base_url=settings.CEREBRAS_BASE_URL,
        api_key=settings.CEREBRAS_API_KEY,
        extra_body={
            "max_tokens": settings.EXTRACTION_MAX_TOKENS,
        },
    )
    response = IdentityResult.model_validate(result.data)
    return _drop_unverified(response, text), result
