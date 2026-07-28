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
        supported = shape_precision(f"{self.value} {self.evidence or ''}")
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


class CVResponse(BaseModel):
    roles: list[ExtractedRole] = Field(default_factory=list)
    skills: list[ExtractedField] = Field(default_factory=list)


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
        },
        "required": ["roles", "skills"],
        "additionalProperties": False,
    }
