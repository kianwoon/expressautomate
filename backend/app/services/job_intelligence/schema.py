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

from re import split as _re_split

from pydantic import BaseModel, Field, field_validator


def _coerce_str_list(value):
    """Coerce a model-provided value into a list[str].

    The LLM occasionally collapses a field the schema declares as an array
    into one joined string (`potential_challenges` shipped the first one: a
    KYC/SOW sentence where a list belonged). Pydantic would refuse it and kill
    a paid analysis at the parse step. Split on arrows/commas/semicolons/
    newlines — the joins models actually use — and drop empties.
    """
    if isinstance(value, str):
        parts = _re_split(r"\s*[→,;]\s*|\n", value)
        return [p.strip() for p in parts if p.strip()]
    return value


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

# allow-hardcode: the LLM-extracted occupation profile (Module 4 step 1). The
# `functions` field is a free-form object of {activity: percentage}; rendered
# as an additional-property object in the schema because the activity names are
# not enumerable ahead of time.
_OCCUPATION_PROFILE_FIELDS = (
    "occupation",
    "seniority",
    "people_management",
    "industry",
)

# allow-hardcode: the re-ranked occupation match (Module 4 step 3). The model
# picks one title from the candidate list and returns confidence + rationale;
# the wage figures and similarity are filled from the candidate row, not the
# model, so only the choice and its justification are asked for.
_OCCUPATION_PICK_FIELDS = (
    "title",
    "confidence",
    "rationale",
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

    # The model occasionally returns a joined string for one of the list
    # fields above (the first production failure: `potential_challenges`
    # arrived as one KYC/SOW sentence). Coerce before type-checking instead
    # of killing a paid analysis at parse time. Applied to every stage below
    # with list fields, since any of them can ship the same surprise.
    _c_daily_activities = field_validator("daily_activities", mode="before")(_coerce_str_list)
    _c_must_have = field_validator("must_have_requirements", mode="before")(_coerce_str_list)
    _c_preferred = field_validator("preferred_requirements", mode="before")(_coerce_str_list)
    _c_success = field_validator("success_characteristics", mode="before")(_coerce_str_list)
    _c_challenges = field_validator("potential_challenges", mode="before")(_coerce_str_list)


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

    _c_backgrounds = field_validator("likely_backgrounds", mode="before")(_coerce_str_list)
    _c_transferable_roles = field_validator("transferable_roles", mode="before")(_coerce_str_list)
    _c_transferable_industries = field_validator("transferable_industries", mode="before")(
        _coerce_str_list
    )
    _c_behaviours = field_validator("behaviours", mode="before")(_coerce_str_list)
    _c_motivations = field_validator("motivations", mode="before")(_coerce_str_list)


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

    _c_queries = field_validator("queries", mode="before")(_coerce_str_list)
    _c_negative_queries = field_validator("negative_queries", mode="before")(_coerce_str_list)


class OccupationProfile(BaseModel):
    """A structured work profile for occupation matching — Module 4 step 1.

    The LLM distils the job order into the facets that distinguish one MOM
    occupation from another: the canonical role name, seniority, whether the
    role manages people, and the industry. `functions` is a free-form weighting
    of activity areas (e.g. {"Recruitment": 40, "Administration": 30}), kept as
    a dict rather than a list so the embedder sees the proportional emphasis,
    not just the labels.
    """

    occupation: str
    functions: dict[str, int] = Field(default_factory=dict)
    seniority: str
    people_management: bool = False
    industry: str


class OccupationMatch(BaseModel):
    """The matched MOM occupation and its wage percentiles — Module 4 result.

    `title`/`year`/the six wage figures come from the `mom_occupations` row the
    re-ranker selected (never from the model — the survey is ground truth, and
    asking the model for wages would invite fabrication). `similarity` is the
    cosine score from the pgvector search; `confidence` and `rationale` are the
    re-ranker's own judgement of how well the title fits the extracted profile.
    """

    title: str
    year: int
    gross_p25: float
    gross_p50: float
    gross_p75: float
    basic_p25: float
    basic_p50: float
    basic_p75: float
    similarity: float = 0.0
    confidence: float = 0.0
    rationale: str = ""


class JobIntelligenceResult(BaseModel):
    """All four stages, as the API returns and the row stores them.

    `occupation` is optional because the match stage degrades to None when the
    reference library is empty or embeddings are unconfigured — the rest of the
    analysis is still useful without a salary benchmark, so a missing match
    fails soft rather than failing the whole run.
    """

    understanding: JDUnderstanding
    persona: CandidatePersona
    search_plan: SearchPlan
    occupation: OccupationMatch | None = None


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
        "occupation_profile": _occupation_profile_schema(),
        "occupation_pick": _schema_for(_OCCUPATION_PICK_FIELDS, confidence=True),
    }


def _schema_for(fields: tuple[str, ...], *, confidence: bool = False) -> dict:
    """One stage's schema, every field typed to match its Pydantic model field.

    `required` lists every property because strict mode allows nothing less, and
    a stage that omits a field from its answer should say so with an empty value
    rather than by silence.

    Each field's JSON type must agree with the Pydantic model: a mismatch sends
    the model one shape and parses it against another, and the model follows the
    schema it was given (it returned `["1"]` for `priority` when the schema said
    array-of-strings but the parser wanted int). Strings in `_SCALAR_FIELDS`,
    integers in `_INTEGER_FIELDS`, booleans in `_BOOLEAN_FIELDS`, `confidence`
    is the lone number, everything else is an array of strings.
    """
    properties: dict[str, object] = {}
    for name in fields:
        if confidence and name == "confidence":
            properties[name] = {"type": "number"}
        elif name in _INTEGER_FIELDS:
            properties[name] = {"type": "integer"}
        elif name in _BOOLEAN_FIELDS:
            properties[name] = {"type": "boolean"}
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


def _occupation_profile_schema() -> dict:
    """The occupation-profile schema, with its free-form `functions` object.

    Unlike the other stages, this one carries a `dict[str, int]` — the
    proportional weighting of activity areas, whose keys are not enumerable
    ahead of time. It is rendered as an additional-properties object so the
    model can name whatever activities the work involves, with integer values
    summing (ideally) to 100. The scalar fields reuse `_SCALAR_FIELDS` typing.
    """
    properties: dict[str, object] = {
        name: {"type": "boolean"} if name in _BOOLEAN_FIELDS else {"type": "string"}
        for name in _OCCUPATION_PROFILE_FIELDS
    }
    properties["functions"] = {
        "type": "object",
        # The keys are activity names; the values are integer weights.
        "additionalProperties": {"type": "integer"},
    }
    required = list(_OCCUPATION_PROFILE_FIELDS) + ["functions"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# Fields whose Pydantic type is `int`, not `str` or `list[str]`. Named once so
# the schema and the Pydantic model agree — see the warning in `_schema_for`.
_INTEGER_FIELDS = {
    "priority",
}


# Fields whose Pydantic type is `bool`. Named once for the same lockstep reason.
_BOOLEAN_FIELDS = {
    "people_management",
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
    # occupation profile
    "occupation",
    "seniority",
    "industry",
    # occupation pick
    "title",
    "rationale",
}
