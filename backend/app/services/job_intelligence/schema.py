"""The model-facing Job Intelligence output contract.

Three Pydantic models — one per pipeline stage — and a `json_schema()` derived
from them that travels *in the prompt* as text, exactly as `ingest/schema.py`
does. The parser is the enforcement; the schema string only tells the model
what to aim for, which no provider can reject for "the compiled grammar is too
large".

None of these fields carry evidence or offsets, unlike `ingest`'s
`ExtractedField`. Those exist there because a field extracted from an email is a
claim about *that email* and must be checkable against it. Here the model is
reasoning about the work, not quoting it, so there is nothing to verify a quote
against — the understanding is an interpretation, the persona an inference, and
the plan a strategy. Confidence lives only where it is honest: a single
self-reported number on the understanding, never on the persona or the plan.
"""

from pydantic import BaseModel, Field

# The fields of each stage, as the model must answer them. Listed in module
# constants rather than reconstructed from the Pydantic model because the
# `json_schema()` below mirrors them by hand, and the two must move together.
# A name here that the model class does not carry (or vice versa) is a defect.

# allow-hardcode: the target shape of the model's answer, not configuration.
_UNDERSTANDING_FIELDS = (
    "role",
    "business_purpose",
    "daily_activities",
    "work_environment",
    "must_have_requirements",
    "preferred_requirements",
    "working_conditions",
    "success_characteristics",
    "potential_challenges",
    "confidence",
)

# allow-hardcode: as above.
_PERSONA_FIELDS = (
    "likely_backgrounds",
    "transferable_roles",
    "transferable_industries",
    "behaviours",
    "communication_style",
    "career_stage",
    "motivations",
    "salary_expectation",
    "availability",
)

# allow-hardcode: as above.
_SEARCH_FIELDS = (
    "platform",
    "priority",
    "queries",
    "negative_queries",
    "salary",
    "location",
    "employment_type",
)


class JDUnderstanding(BaseModel):
    """What the work is — Module 1 of the design doc."""

    role: str
    business_purpose: str
    daily_activities: list[str] = Field(default_factory=list)
    work_environment: str
    must_have_requirements: list[str] = Field(default_factory=list)
    preferred_requirements: list[str] = Field(default_factory=list)
    working_conditions: str
    success_characteristics: list[str] = Field(default_factory=list)
    potential_challenges: list[str] = Field(default_factory=list)
    # The model's own estimate of how well-formed the source job order was.
    # 0.0–1.0. Honest as a self-report only — never rendered as a probability
    # a recruiter acts on without reading the rest.
    confidence: float = 0.0


class CandidatePersona(BaseModel):
    """Who would do this work well — Module 2 of the design doc.

    Describes the *ideal person for the work*, never a specific candidate. It
    is deliberately not bound to any row in `candidates`: the whole point is
    that the platform then goes looking for people who resemble this.
    """

    likely_backgrounds: list[str] = Field(default_factory=list)
    transferable_roles: list[str] = Field(default_factory=list)
    transferable_industries: list[str] = Field(default_factory=list)
    behaviours: list[str] = Field(default_factory=list)
    communication_style: str
    career_stage: str
    motivations: list[str] = Field(default_factory=list)
    salary_expectation: str
    availability: str


class SearchPlan(BaseModel):
    """How to look for that person — Module 3 of the design doc.

    The platform-specific drivers (FastJobs, MyCareersFuture) are Phase 2; this
    is the strategy those drivers will consume. `queries` and `negative_queries`
    are boolean-style search strings a platform driver can translate.
    """

    platform: str
    # 1 (highest) to 5. Lets a recruiter rank several plans against one role.
    priority: int = 3
    queries: list[str] = Field(default_factory=list)
    negative_queries: list[str] = Field(default_factory=list)
    salary: str
    location: str
    employment_type: str


class JobIntelligenceResult(BaseModel):
    """All three stages, as the API returns and the row stores them."""

    understanding: JDUnderstanding
    persona: CandidatePersona
    search_plan: SearchPlan


def json_schema() -> dict:
    """The schema sent to the model as prompt text.

    Hand-built to satisfy strict structured output (`additionalProperties: false`
    and `required` naming every property), and written as a flat, readable
    object a model can hold in one pass. List fields use `{"type": "array",
    "items": {"type": "string"}}`; everything else is a string. `confidence` is
    the lone number.

    One stage at a time is requested — never all three in one call — so each
    stage's schema is returned by its own module calling the matching helper
    below. That is what keeps the stages "independent and testable": a test
    asserts on one prompt's shape, not on a merged document.
    """
    return {
        "understanding": _schema_for(_UNDERSTANDING_FIELDS, confidence=True),
        "persona": _schema_for(_PERSONA_FIELDS),
        "search": _schema_for(_SEARCH_FIELDS),
    }


def _schema_for(fields: tuple[str, ...], *, confidence: bool = False) -> dict:
    """One stage's schema, every field a string array unless noted.

    `required` lists every property because strict mode allows nothing less, and
    a stage that omits a field from its answer should say so with an empty value
    rather than by silence.
    """
    properties: dict[str, object] = {}
    for name in fields:
        if confidence and name == "confidence":
            properties[name] = {"type": "number"}
        elif name in _SCALAR_FIELDS:
            properties[name] = {"type": "string"}
        else:
            properties[name] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": properties,
        "required": list(fields),
        "additionalProperties": False,
    }


# Fields that are single strings, not arrays. Named once so the schema and the
# Pydantic model agree on which fields are which shape.
_SCALAR_FIELDS = {
    # understanding
    "role",
    "business_purpose",
    "work_environment",
    "working_conditions",
    # persona
    "communication_style",
    "career_stage",
    "salary_expectation",
    "availability",
    # search
    "platform",
    "salary",
    "location",
    "employment_type",
}
