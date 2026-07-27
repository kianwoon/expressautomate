"""The model-facing extraction contract (plan §13, §15).

Every field carries its evidence *and* the character offsets that evidence came
from. Asking for evidence alone is not verification — a model can invent a
plausible quote as easily as a plausible salary. Offsets are checkable; prose
is not.
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
    def _present_values_must_be_locatable(self) -> "ExtractedField":
        """A value with no offsets cannot be verified, so it is not accepted.

        This is the schema-level half of the no-fabrication rule; the other
        half is checking the offsets against the source in evidence.py.
        """
        if self.is_missing:
            return self
        if self.start_char is None or self.end_char is None:
            raise ValueError(f"{self.value!r} has no source offsets")
        if self.end_char <= self.start_char:
            raise ValueError(f"offsets out of order: {self.start_char}..{self.end_char}")
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
    """Schema sent to the model. Derived, so it cannot drift from the parser."""
    field_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "evidence": {"type": "string"},
            "start_char": {"type": "integer"},
            "end_char": {"type": "integer"},
            "confidence": {"type": "number"},
        },
        "required": ["value"],
    }
    return {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": dict.fromkeys(FIELDS, field_schema),
                },
            }
        },
        "required": ["jobs"],
    }
