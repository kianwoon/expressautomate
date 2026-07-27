"""The model-facing extraction contract (plan §13, §15).

Every field carries the text it was taken from. The quote is what makes the
no-fabrication rule mechanical: evidence.py looks for it in the email and the
field is only trusted when it is found there.

Offsets used to be part of the contract too, and that was a mistake measured
against real responses. Models quote accurately and count characters badly, so
a correct extraction of a long `job_description` was thrown away for offsets
that were a few characters out. Locating a quote is something code does
perfectly and a language model does not, so the work moved: the model quotes,
evidence.py finds. What arrives here is a hint for disambiguating a quote that
occurs twice, never a claim that has to be true.
"""

from pydantic import BaseModel, Field, model_validator

NOT_MENTIONED = "Not mentioned"

# allow-hardcode: the target columns of `opportunities`, not configuration. A
# name here that no column matches is a defect, so this list moves with the
# migration rather than with `.env`.
FIELDS = (
    "company",
    "job_title",
    "job_description",
    "requirements",
    "salary",
    "salary_period",
    "working_hours",
    "work_arrangement",
    "employment_type",
    "duration",
    "location",
    "skills",
)


class ExtractedField(BaseModel):
    value: str
    evidence: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    confidence: float = 0.0

    @property
    def is_missing(self) -> bool:
        return self.value.strip().lower() == NOT_MENTIONED.lower()

    @model_validator(mode="after")
    def _present_values_must_quote_something(self) -> "ExtractedField":
        """A value with no quotation behind it is not accepted.

        This is the whole anti-fabrication mechanism, so it stays strict.
        `value` is legitimately allowed to differ from the source text ("Up to
        3500" for "Up to $3,500"), which means the only thing verification can
        compare the email against is the model's own quotation. With `evidence`
        optional there would be nothing to check at all.

        Nothing else is required. Offsets are a hint (see the module docstring)
        and an absent or wrong one costs the field nothing; a quote that is not
        in the email is what costs it its trust, and only evidence.py — which
        has the email — can decide that.
        """
        if self.is_missing:
            return self
        if not (self.evidence or "").strip():
            raise ValueError(f"{self.value!r} quotes no source text")
        return self


class ExtractedJob(BaseModel):
    company: ExtractedField | None = None
    job_title: ExtractedField | None = None
    job_description: ExtractedField | None = None
    requirements: ExtractedField | None = None
    salary: ExtractedField | None = None
    salary_period: ExtractedField | None = None
    working_hours: ExtractedField | None = None
    work_arrangement: ExtractedField | None = None
    employment_type: ExtractedField | None = None
    duration: ExtractedField | None = None
    location: ExtractedField | None = None
    skills: ExtractedField | None = None


class ExtractionResponse(BaseModel):
    jobs: list[ExtractedJob] = Field(default_factory=list)


def json_schema() -> dict:
    """Schema sent to the model. Derived, so it cannot drift from the parser.

    Sent as *text inside the prompt* rather than as a `json_schema` response
    format. Twelve nested objects is a large grammar, and a provider that
    compiles the schema into one refused this exact document: "the compiled
    grammar is too large". That failure is uniform — every email, every time —
    so extraction asks for a plain JSON object and states the shape in prose,
    which no provider can reject. The parser above is still the enforcement;
    the schema here only tells the model what to aim for.

    Still written to satisfy **strict** structured output: every object sets
    `additionalProperties: false` and `required` names *every* property. That
    costs nothing as prompt text and keeps the document usable as a real
    response format on a provider that can compile it.

    Optionality is therefore expressed in the type, not by omission from
    `required`: a field the email does not mention comes back as `null`. That
    is a better contract anyway — the model answers for all twelve fields
    every time, so "not mentioned" is a statement rather than a silence that
    could equally mean the model forgot.
    """
    field_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "evidence": {"type": "string"},
            "start_char": {"type": "integer"},
            "end_char": {"type": "integer"},
            "confidence": {"type": "number"},
        },
        # Every key, because strict mode allows nothing less. Asking only for
        # `value` let the model return a bare string it could not be held to,
        # and the parser then rejected the whole response — strictness landing
        # as a failed extraction rather than as guidance the model could follow.
        "required": ["value", "evidence", "start_char", "end_char", "confidence"],
        "additionalProperties": False,
    }
    # A field may be absent from an email; it may never be absent from the
    # answer. `["object", "null"]` is how strict mode says that.
    nullable_field = {**field_schema, "type": ["object", "null"]}

    return {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": dict.fromkeys(FIELDS, nullable_field),
                    "required": list(FIELDS),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["jobs"],
        "additionalProperties": False,
    }
