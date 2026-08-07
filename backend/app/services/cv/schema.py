"""The model-facing CV extraction contract (plan §13, §15).

The shape is the vacancy contract's, said about a different document: every
field carries the text it was taken from, and the quotation is what makes the
no-fabrication rule mechanical — `ingest.evidence.verify` looks for it in the
CV and the field is only trusted when it is found there.

`ExtractedField` is inherited rather than copied. It could have been restated
here in ten lines, but the validator on it *is* the anti-fabrication rule, and
a second copy of a rule is a second place for it to be weakened by someone who
only reads one of them. Inheriting also guarantees the thing the pipeline
depends on: `verify()` was written against that class, and a look-alike that
drifts by one attribute name would fail silently by verifying nothing.

Dates are the one addition. A CV states a date at whatever precision its author
felt like — "2019", "Mar 2019", "3 March 2019" — and the damaging move is to
round that up to a full date because a column wants one. `precision` records
what the page actually carried, and `_no_finer_than_the_page` refuses a claim
the quoted text cannot support, so an invented day is a rejected response
rather than a stored fact.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.services.ingest.schema import ExtractedField

# Coarsest first, so a comparison by index is a comparison by precision.
PRECISIONS = ("year", "month", "day")

# allow-hardcode: the vocabulary of written dates, not configuration.
_MONTH = (
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
)
_YEAR = r"\d{4}"
# A day is only a day when a day *number* is written next to a month: "3 March
# 2019", "March 3, 2019", "2019-03-03". Anything less is not one, and the whole
# point of this module is that we do not fill the gap in.
_DAY_SHAPED = re.compile(
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+{_MONTH}\b"
    rf"|\b{_MONTH}\s+\d{{1,2}}(st|nd|rd|th)?\b\s*,?\s*{_YEAR}"
    rf"|\b{_YEAR}[-/]\d{{1,2}}[-/]\d{{1,2}}\b"
    rf"|\b\d{{1,2}}[-/]\d{{1,2}}[-/]{_YEAR}\b",
    re.IGNORECASE,
)
_MONTH_SHAPED = re.compile(
    rf"\b{_MONTH}\b|\b{_YEAR}[-/]\d{{1,2}}\b|\b\d{{1,2}}[-/]{_YEAR}\b",
    re.IGNORECASE,
)


def shape_precision(text: str) -> str:
    """The finest precision the written text could honestly be read at."""
    if _DAY_SHAPED.search(text):
        return "day"
    if _MONTH_SHAPED.search(text):
        return "month"
    return "year"


class ExtractedDate(ExtractedField):
    """A date, plus how exactly the page gave it.

    `precision` is required of a date that is present. Optional, it would come
    back absent for the ambiguous cases and present for the obvious ones, which
    is precisely backwards: the caller needs to know the resolution most when
    the model was least sure of it.
    """

    precision: Literal["year", "month", "day"] | None = None

    @model_validator(mode="after")
    def _no_finer_than_the_page(self) -> "ExtractedDate":
        """Refuse a resolution the quoted text does not contain.

        A model asked for "the precision actually seen" will still sometimes
        answer `day` for "Mar 2019", and if that is accepted the invented 1st
        arrives downstream indistinguishable from a date the candidate wrote.
        Raising instead makes the response unusable, which is the one signal
        `extract_cv` acts on — the answer is re-asked, never quietly patched.
        """
        if self.is_missing:
            return self
        if self.precision is None:
            raise ValueError(f"{self.value!r} states no date precision")
        # `value` is the model's claim, not evidence of anything — concatenating
        # it in here would let a fabricated day in `value` license its own
        # precision, since the day the model invented would then appear in the
        # very string being inspected for a day. Only `evidence`, the quote off
        # the page, may say what precision the page actually supports.
        supported = shape_precision(self.evidence or "")
        if PRECISIONS.index(self.precision) > PRECISIONS.index(supported):
            raise ValueError(
                f"{self.value!r} is {supported} precision, not {self.precision}"
            )
        return self


class ExtractedRole(BaseModel):
    """One position held. Every part of it optional, because a CV is prose.

    Plenty of real CVs name a company with no title, or a title with no dates.
    Requiring a field here would reject the document rather than record what it
    honestly said, and `extract_cv` already drops a role whose quotations do
    not check out.
    """

    title: ExtractedField | None = None
    company: ExtractedField | None = None
    start_date: ExtractedDate | None = None
    end_date: ExtractedDate | None = None
    summary: ExtractedField | None = None


# The salary periods a CHECK constraint allows. Mirrors the vocabulary in
# `ingest.evidence` — a period outside these is refused to NULL, never guessed.
SALARY_PERIODS = ("hour", "day", "week", "month", "year")


class ExtractedSalary(BaseModel):
    """A salary the candidate stated, read by the model into its parts.

    The model reads "$ 5000 x 12" and returns amount=5000, currency="SGD",
    period="month" — the interpretation a regex cannot reliably make, because
    the same notation means different things in different contexts ("x 12" is
    months here, a multiplier elsewhere). The model understands it; the code
    trusts the model only after `verify` finds its `evidence` quote on the page.

    `amount` is the figure in the stated period (5000/month, not 60000/year),
    so the number the recruiter sees is the number the candidate wrote.
    Currency defaults to SGD for this Singapore platform when the CV uses a
    bare "$" — the model is told this, because a deterministic parser cannot
    tell which "$" is SGD and which is USD, but the model can read the CV's
    country context.
    """

    amount: float | None = None
    currency: str | None = None
    period: str | None = None
    evidence: str | None = None
    confidence: float = 0.0

    @property
    def is_missing(self) -> bool:
        """A salary with no amount is "Not mentioned", same sentinel rule."""
        return self.amount is None

    @model_validator(mode="after")
    def _present_salary_must_quote_something(self) -> "ExtractedSalary":
        """Same anti-fabrication rule as ExtractedField: quote or refuse."""
        if self.is_missing:
            return self
        if not (self.evidence or "").strip():
            raise ValueError(f"salary amount {self.amount} quotes no source text")
        if self.period is not None and self.period not in SALARY_PERIODS:
            raise ValueError(
                f"period {self.period!r} is not one of {SALARY_PERIODS}"
            )
        return self


class CVResponse(BaseModel):
    """Roles, skills, and the salary the candidate stated — and nothing else.

    A CV very often states sex, date of birth, nationality and race outright —
    for a domestic-worker placement it nearly always does. None of them is
    asked for here, and none may be added: `cv_json_schema()` is derived from
    this class, so a field added here is a field the model is instructed to
    extract, and an AI-asserted protected characteristic is exactly what
    `app/services/sourcing/redact.py` refuses to let this platform hold.
    Those columns on `candidates` are filled by a person, deliberately, or
    they stay NULL (§15).

    Salary is the one exception to "nothing else about the person", because a
    figure the candidate printed is a fact they stated, not a characteristic
    to be inferred. Both last-drawn and expected are single quoted strings —
    verbatim from the page, like every role field — parsed into amount,
    currency and period by `persist_cv`, never by the model.
    """

    roles: list[ExtractedRole] = Field(default_factory=list)
    skills: list[ExtractedField] = Field(default_factory=list)
    last_drawn_salary: ExtractedSalary | None = None
    expected_salary: ExtractedSalary | None = None


# The field names in the order the prompt should present them.
ROLE_FIELDS = tuple(ExtractedRole.model_fields)


def cv_json_schema() -> dict:
    """Schema sent to the model. Derived, so it cannot drift from the parser.

    Sent as *text inside the prompt*, not as a `json_schema` response format,
    for the reason recorded in `ingest/schema.py`: a provider that compiles the
    schema refused the vacancy document outright — "the compiled grammar is too
    large" — and that failure was uniform across every email. Extraction here
    asks for a plain JSON object and states the shape in prose, which nothing
    compiles and nothing can refuse.

    Still written to satisfy strict structured output — every object sets
    `additionalProperties: false` and `required` names every property — so the
    document stays usable as a real response format on a provider that can
    compile it. Optionality therefore lives in the type: a field the CV does
    not state comes back as an object whose value is "Not mentioned", never as
    a silence that could equally mean the model forgot.
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
    date_schema = {
        **field_schema,
        "properties": {
            **field_schema["properties"],
            "precision": {"type": ["string", "null"], "enum": [*PRECISIONS, None]},
        },
        "required": [*field_schema["required"], "precision"],
    }
    nullable = {**field_schema, "type": ["object", "null"]}
    nullable_date = {**date_schema, "type": ["object", "null"]}
    salary_schema = {
        "type": ["object", "null"],
        "properties": {
            "amount": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
            "period": {"type": ["string", "null"], "enum": [*SALARY_PERIODS, None]},
            "evidence": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["amount", "currency", "period", "evidence", "confidence"],
        "additionalProperties": False,
    }

    return {
        "type": "object",
        "properties": {
            "roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": nullable,
                        "company": nullable,
                        "start_date": nullable_date,
                        "end_date": nullable_date,
                        "summary": nullable,
                    },
                    "required": list(ROLE_FIELDS),
                    "additionalProperties": False,
                },
            },
            "skills": {"type": "array", "items": field_schema},
            "last_drawn_salary": salary_schema,
            "expected_salary": salary_schema,
        },
        "required": ["roles", "skills", "last_drawn_salary", "expected_salary"],
        "additionalProperties": False,
    }
