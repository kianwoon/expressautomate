"""The model-facing Candidate Intelligence v2 output contract.

Two-stage engine, rebuilt sharp per the revised design doc:
  Pass 1 — WORK: decompose every role into work units, each with a decision-
            ownership level (0-5), a complexity classification (operational /
            skilled / specialist / expert), an evidence level (A-E), and an AI
            heavy-lift classification. Title-blind and tenure-aware.
  Pass 2 — ASSESSMENT: a blunt synthesis — headline conclusion, what's scarce,
            what's depreciated, what's unproven, how fast they could contribute.

The two load-bearing dimensions (revised doc §6, §10A):

**Decision ownership** (0-5): executes → provides input → recommends → decides
→ owns → designs. "Prepared risk submissions" is 1-2; "owned risk decisions"
is 3-4; "designed the underwriting framework" is 5. Do NOT infer 3-5 without
evidence.

**AI heavy-lift** (5 levels): AI-independent → AI-assisted → AI-heavy-lift →
AI-dominant → AI-agentic. Not "can AI replace the job" but "can AI do the
HEAVY LIFT of this work unit" — if AI does 80% of the substantive work, the
human's value is compressed even though they're still "involved".

**Claim vs substance**: the engine must detect inflated CV language — a grand
sentence over surface work. Where the claim outweights the substance, evidence
drops to C (claimed but insufficiently demonstrated) and the note says why.
This is the recruiter's single most useful signal.
"""

from pydantic import BaseModel, Field, field_validator


def _coerce_str_list(value):
    """Coerce a model-provided value into a list[str].

    The LLM sometimes returns a joined string for a field the schema declares
    as an array. This splits it on arrows/commas/semicolons/newlines.
    """
    if isinstance(value, str):
        import re

        parts = re.split(r"\s*[→,;]\s*|\n", value)
        return [p.strip() for p in parts if p.strip()]
    return value


# ---------------------------------------------------------------------------
# Pass 1 — WORK DECOMPOSITION
# ---------------------------------------------------------------------------


class WorkUnit(BaseModel):
    """One concrete piece of work, decomposed to its actual substance.

    This is where the revised doc's intelligence lives. Each work unit carries:
    - `claim`: the CV's own words (what it SAYS the person did).
    - `work`: the actual work underneath (what they REALLY did — the
      operational verb, stripped of inflation).
    - `decision_ownership`: the 0-5 scale (revised doc §6). THE core dimension.
    - `complexity`: operational / skilled / specialist / expert (revised doc §9).
    - `ai_heavy_lift`: the AI heavy-lift classification (revised doc §10A).
    - `evidence`: A-E (revised doc §14). Where the claim is grander than the
      substance, this drops to C and `evidence_note` says why.
    - `inflated`: true when the CV language overstates the work — the signal a
      recruiter needs most.
    """

    claim: str = ""
    work: str = ""
    decision_ownership: str = ""
    complexity: str = ""
    ai_heavy_lift: str = ""
    human_residual: str = ""
    evidence: str = ""
    evidence_note: str = ""
    inflated: bool = False


class RoleAssessment(BaseModel):
    """One employment period, with its work units decomposed and assessed.

    The title is retained for chronology (revised doc Rule 1: ignore the title
    as evidence, keep it for context). `contribution_maturity` captures how far
    the person reached in the ramp-up → assisted → independent → ownership →
    expert → design arc (revised doc §8) — independent of raw tenure.
    """

    employer: str = ""
    period: str = ""
    stated_title: str = ""
    industry: str = ""
    work_units: list[WorkUnit] = Field(default_factory=list)
    contribution_maturity: str = ""
    tenure_months: int = 0


class EducationEntry(BaseModel):
    """One education or qualification entry."""

    period: str = ""
    qualification: str = ""
    institution: str = ""
    field: str = ""


class WorkAssessment(BaseModel):
    """Pass 1 output — the full work decomposition.

    Value-neutral: this states the work and its decision-ownership/complexity/
    AI-exposure per unit. It does NOT yet synthesise residual value (Pass 2
    does). Education is captured here because it's a factual extraction.
    """

    roles: list[RoleAssessment] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pass 2 — ASSESSMENT (the sharp synthesis)
# ---------------------------------------------------------------------------


class DepreciatedCapability(BaseModel):
    """A capability whose market value has declined (revised doc §12).

    `reason` is mandatory — a depreciation without a reason is a silent verdict.
    """

    capability: str = ""
    reason: str = ""


class ScarceCapability(BaseModel):
    """A capability that remains economically scarce (revised doc §13).

    `evidence` is mandatory — a residual-value claim without evidence is
    unsupported inference (guardrail 6).
    """

    capability: str = ""
    evidence: str = ""


class UnprovenClaim(BaseModel):
    """A claim the CV makes that the work underneath does not support.

    These are the interview questions a recruiter should ask. "Not evidenced !=
    does not possess" (revised doc §14) — these are verification items, not
    rejections.
    """

    claim: str = ""
    question: str = ""


class CandidateAssessment(BaseModel):
    """Pass 2 output — the sharp, blunt synthesis.

    `headline` is the one-line read a recruiter opens with. `summary` is the
    3-4 sentence candid profile. Everything else is the decomposable evidence
    behind them. NO opaque single score, NO hedging, NO corporate language.
    """

    headline: str = ""
    summary: str = ""
    work_level: str = ""
    decision_authority: str = ""
    scarce_capabilities: list[ScarceCapability] = Field(default_factory=list)
    depreciated_capabilities: list[DepreciatedCapability] = Field(
        default_factory=list
    )
    unproven_claims: list[UnprovenClaim] = Field(default_factory=list)
    ai_exposure: str = ""
    hire_readiness: str = ""
    value_trajectory: str = ""

    _c_ai = field_validator("ai_exposure", mode="before")(
        classmethod(lambda cls, v: v if isinstance(v, str) else str(v))
    )


class CandidateIntelligenceResult(BaseModel):
    """Both stages, as the API returns and the row stores them."""

    work: WorkAssessment
    assessment: CandidateAssessment


# ---------------------------------------------------------------------------
# Hand-built JSON schema (travels in the prompt as text; Pydantic enforces)
# ---------------------------------------------------------------------------

# allow-hardcode: the target shape of the model's answer, not configuration.
_WORK_UNIT_FIELDS = (
    "claim",
    "work",
    "decision_ownership",
    "complexity",
    "ai_heavy_lift",
    "human_residual",
    "evidence",
    "evidence_note",
    "inflated",
)

# allow-hardcode: as above.
_ROLE_FIELDS = (
    "employer",
    "period",
    "stated_title",
    "industry",
    "work_units",
    "contribution_maturity",
    "tenure_months",
)

# allow-hardcode: as above.
_EDUCATION_FIELDS = (
    "period",
    "qualification",
    "institution",
    "field",
)

# allow-hardcode: as above.
_SCARCE_FIELDS = (
    "capability",
    "evidence",
)

# allow-hardcode: as above.
_DEPRECIATED_FIELDS = (
    "capability",
    "reason",
)

# allow-hardcode: as above.
_UNPROVEN_FIELDS = (
    "claim",
    "question",
)

# The scalar (single-string) fields across every stage.
# allow-hardcode: the scalar field names, not configuration.
_SCALAR_FIELDS = set(
    [
        # work unit
        "claim",
        "work",
        "decision_ownership",
        "complexity",
        "ai_heavy_lift",
        "human_residual",
        "evidence",
        "evidence_note",
        # role
        "employer",
        "period",
        "stated_title",
        "industry",
        "contribution_maturity",
        # education
        "qualification",
        "institution",
        "field",
        # scarce / depreciated / unproven
        "capability",
        "reason",
        "question",
        # assessment
        "headline",
        "summary",
        "work_level",
        "decision_authority",
        "ai_exposure",
        "hire_readiness",
        "value_trajectory",
    ]
)

# The integer fields (typed as number in the JSON schema).
# allow-hardcode: as above.
_INTEGER_FIELDS = {
    "tenure_months",
    "inflated",
}


def json_schema() -> dict:
    """The schema sent to the model as prompt text."""
    return {
        "work": _work_schema(),
        "assessment": _assessment_schema(),
    }


def _work_unit_schema() -> dict:
    properties: dict[str, object] = {}
    for name in _WORK_UNIT_FIELDS:
        if name in _INTEGER_FIELDS:
            properties[name] = {"type": "boolean"}
        elif name in _SCALAR_FIELDS:
            properties[name] = {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _role_schema() -> dict:
    scalar_fields = [f for f in _ROLE_FIELDS if f not in ("work_units", "tenure_months")]
    properties: dict[str, object] = {name: {"type": "string"} for name in scalar_fields}
    properties["tenure_months"] = {"type": "integer"}
    properties["work_units"] = {
        "type": "array",
        "items": _work_unit_schema(),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _education_schema() -> dict:
    properties: dict[str, object] = {name: {"type": "string"} for name in _EDUCATION_FIELDS}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _work_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "roles": {"type": "array", "items": _role_schema()},
            "education": {"type": "array", "items": _education_schema()},
        },
        "required": ["roles", "education"],
        "additionalProperties": False,
    }


def _scarce_schema() -> dict:
    properties: dict[str, object] = {name: {"type": "string"} for name in _SCARCE_FIELDS}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _depreciated_schema() -> dict:
    properties: dict[str, object] = {name: {"type": "string"} for name in _DEPRECIATED_FIELDS}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _unproven_schema() -> dict:
    properties: dict[str, object] = {name: {"type": "string"} for name in _UNPROVEN_FIELDS}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _assessment_schema() -> dict:
    scalar_fields = [
        f
        for f in (
            "headline",
            "summary",
            "work_level",
            "decision_authority",
            "ai_exposure",
            "hire_readiness",
            "value_trajectory",
        )
    ]
    properties: dict[str, object] = {name: {"type": "string"} for name in scalar_fields}
    properties["scarce_capabilities"] = {
        "type": "array",
        "items": _scarce_schema(),
    }
    properties["depreciated_capabilities"] = {
        "type": "array",
        "items": _depreciated_schema(),
    }
    properties["unproven_claims"] = {
        "type": "array",
        "items": _unproven_schema(),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }
