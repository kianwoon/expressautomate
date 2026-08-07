"""The model-facing Candidate Intelligence output contract.

Three Pydantic models — one per pipeline stage — and a `json_schema()` derived
from them that travels *in the prompt* as text, exactly as the Job Intelligence
schema does. The parser is the enforcement; the schema string only tells the
model what to aim for, which no provider can reject for "the compiled grammar
is too large".

`supporting_evidence` carries a short verbatim quote from the CV, unlike the
Job Intelligence fields (which carry no evidence at all). The difference is
that a CV is a source document the model is reasoning *about*, so a capability
claim that names where in the CV it came from is both more useful and more
honest than an unsupported adjective. It is a free-form quote string, not the
offset/`verify` machinery `ingest`'s `ExtractedField` uses: this is
interpretation written to JSONB, not a persisted structured fact, so the heavy
anti-fabrication defense that guards the structured rows does not apply here.
The light quote satisfies the design doc's "every inference needs provenance"
without the overhead of re-locating each phrase against the source bytes.
"""

from pydantic import BaseModel, Field

# The fields of each stage, as the model must answer them. Listed in module
# constants rather than reconstructed from the Pydantic model because the
# `json_schema()` below mirrors them by hand, and the two must move together.
# A name here that the model class does not carry (or vice versa) is a defect.

# allow-hardcode: the target shape of the model's answer, not configuration.
_CAREER_FIELDS = (
    "timeline",
    "trajectory",
    "primary_domain",
    "secondary_domains",
    "career_direction",
    "career_stage",
)

# allow-hardcode: as above. The capability entries are nested objects, handled
# separately from the flat scalar/array typing the rest of the fields use.
_CAPABILITY_FIELDS = (
    "capabilities",
    "tools",
)

# allow-hardcode: as above.
_PROFILE_FIELDS = (
    "professional_identity",
    "specializations",
    "orientation",
    "role_affinity",
)

# The scalar (single-string) fields of a capability entry. The `confidence`
# field is the lone number and `supporting_evidence` is a string; `capability`
# and `category` are also strings.
# allow-hardcode: the nested-object field shape, not configuration.
_CAPABILITY_ENTRY_FIELDS = (
    "capability",
    "category",
    "confidence",
    "supporting_evidence",
)

# The scalar fields of a role-affinity entry. `confidence` is the lone number.
# allow-hardcode: as above.
_ROLE_AFFINITY_ENTRY_FIELDS = (
    "role",
    "affinity_type",
    "confidence",
)


class TimelineEntry(BaseModel):
    """One rung on the chronological career ladder.

    `period` is a free-form string (e.g. "2019–2023") because CV date precision
    varies; normalising here would lose information the model can preserve.
    """

    period: str = ""
    title: str = ""
    domain: str = ""


class CareerProfile(BaseModel):
    """Stage 1 — the candidate's career, as a structured progression.

    The career timeline and trajectory are first-class objects (design doc
    Phase 3), not derived display text, because matching reasons about
    transferable experience even when the current title differs from the target.
    """

    timeline: list[TimelineEntry] = Field(default_factory=list)
    # The ordered progression of domains the candidate moved through, oldest
    # first — the trajectory diagram the design doc (Phase 6) describes.
    trajectory: list[str] = Field(default_factory=list)
    primary_domain: str
    secondary_domains: list[str] = Field(default_factory=list)
    career_direction: str
    career_stage: str


class CapabilityEntry(BaseModel):
    """One capability the candidate has demonstrated, with its backing evidence.

    `category` groups capabilities the way the design doc (Phase 4) does:
    `domain`, `functional`, or `operational`. `confidence` (0.0–1.0) is the
    model's honest estimate of how well the CV supports the claim, derived from
    directness, recency, and duration. `supporting_evidence` is a short verbatim
    quote from the CV — provenance without the offset/verify machinery.
    """

    capability: str
    category: str
    confidence: float = 0.0
    supporting_evidence: str = ""


class CapabilityProfile(BaseModel):
    """Stage 2 — what the candidate can actually do, evidence-backed.

    Capabilities are grouped by category rather than a flat skill list, because
    "Commercial underwriting (functional, 0.98)" tells a recruiter more than a
    bare noun. A capability should ideally be supported by an action or repeated
    experience, not just a noun in a CV — the prompt enforces this.
    """

    capabilities: list[CapabilityEntry] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class RoleAffinity(BaseModel):
    """One role the candidate could plausibly fit, with a fit classification.

    `affinity_type` is `direct_fit` (current title matches), `adjacent` (close
    but not exact), or `transferable` (different title, shared capabilities).
    Role affinity is a model hypothesis, not a factual statement about the
    candidate — the prompt says so, and the UI should present it as such.
    """

    role: str
    affinity_type: str
    confidence: float = 0.0


class ProfessionalProfile(BaseModel):
    """Stage 3 — what kind of professional this candidate is.

    The candidate equivalent of the Job Intelligence "Person" view, but a
    synthesis of *this* candidate's career and capability evidence rather than
    an ideal-person inference. It should not simply repeat the latest job title.
    """

    professional_identity: str
    specializations: list[str] = Field(default_factory=list)
    orientation: str
    role_affinity: list[RoleAffinity] = Field(default_factory=list)


class CandidateIntelligenceResult(BaseModel):
    """All three stages, as the API returns and the row stores them."""

    career: CareerProfile
    capability: CapabilityProfile
    profile: ProfessionalProfile


def json_schema() -> dict:
    """The schema sent to the model as prompt text.

    Hand-built to satisfy strict structured output (`additionalProperties: false`
    and `required` naming every property), and written as a flat, readable
    object a model can hold in one pass. One stage at a time is requested —
    never all three in one call — so each stage's schema is returned by its own
    module calling the matching helper below.
    """
    return {
        "career": _career_schema(),
        "capability": _capability_schema(),
        "profile": _profile_schema(),
    }


def _career_schema() -> dict:
    """The career stage schema, with its nested timeline entries.

    `timeline` is an array of objects, each `{period, title, domain}`. The rest
    are typed to match their Pydantic model fields: strings in `_SCALAR_FIELDS`,
    arrays of strings otherwise.
    """
    properties: dict[str, object] = {
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "period": {"type": "string"},
                    "title": {"type": "string"},
                    "domain": {"type": "string"},
                },
                "required": ["period", "title", "domain"],
                "additionalProperties": False,
            },
        },
    }
    for name in _CAREER_FIELDS:
        if name == "timeline":
            continue
        properties[name] = (
            {"type": "string"}
            if name in _SCALAR_FIELDS
            else {"type": "array", "items": {"type": "string"}}
        )
    required = [f for f in _CAREER_FIELDS if f != "timeline"] + ["timeline"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _capability_schema() -> dict:
    """The capability stage schema, with its nested capability entries.

    `capabilities` is an array of objects, each carrying a `confidence` number
    and a `supporting_evidence` string alongside the `capability`/`category`
    strings. `tools` is a plain array of strings.
    """
    entry_properties: dict[str, object] = {}
    for name in _CAPABILITY_ENTRY_FIELDS:
        if name == "confidence":
            entry_properties[name] = {"type": "number"}
        else:
            entry_properties[name] = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "capabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": entry_properties,
                    "required": list(_CAPABILITY_ENTRY_FIELDS),
                    "additionalProperties": False,
                },
            },
            "tools": {"type": "array", "items": {"type": "string"}},
        },
        "required": list(_CAPABILITY_FIELDS),
        "additionalProperties": False,
    }


def _profile_schema() -> dict:
    """The profile stage schema, with its nested role-affinity entries.

    `role_affinity` is an array of objects, each carrying a `confidence` number
    alongside the `role`/`affinity_type` strings. The rest are typed to match
    their Pydantic model fields.
    """
    affinity_properties: dict[str, object] = {}
    for name in _ROLE_AFFINITY_ENTRY_FIELDS:
        if name == "confidence":
            affinity_properties[name] = {"type": "number"}
        else:
            affinity_properties[name] = {"type": "string"}
    properties: dict[str, object] = {
        "role_affinity": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": affinity_properties,
                "required": list(_ROLE_AFFINITY_ENTRY_FIELDS),
                "additionalProperties": False,
            },
        },
    }
    for name in _PROFILE_FIELDS:
        if name == "role_affinity":
            continue
        properties[name] = (
            {"type": "string"}
            if name in _SCALAR_FIELDS
            else {"type": "array", "items": {"type": "string"}}
        )
    required = [f for f in _PROFILE_FIELDS if f != "role_affinity"] + ["role_affinity"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# Fields that are single strings, not arrays. Named once so the schema and the
# Pydantic model agree on which fields are which shape — see the warning above.
# allow-hardcode: the scalar field names, not configuration.
_SCALAR_FIELDS = {
    # career
    "primary_domain",
    "career_direction",
    "career_stage",
    "period",
    "title",
    "domain",
    # capability entry
    "capability",
    "category",
    "supporting_evidence",
    # profile
    "professional_identity",
    "orientation",
    "role",
    "affinity_type",
}
