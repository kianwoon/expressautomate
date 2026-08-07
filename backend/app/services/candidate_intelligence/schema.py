"""The model-facing Candidate Intelligence v2 output contract.

The engine reassesses a candidate's historical experience against today's
labour market and derives a candid view of their current economic value. The
core principle (design doc §1) is:

    Experience is evidence, not value.

Years of experience must not be treated as a proxy for current market value.
The engine therefore re-prices historical work against today's automation/AI
reality and surfaces what remains scarce and economically useful.

Five Pydantic models — one per pipeline stage — and a `json_schema()` derived
from them that travels *in the prompt* as text, exactly as the Job Intelligence
schema does. The Pydantic parser is the enforcement; the schema string only
tells the model what to aim for, which no provider can reject for "the compiled
grammar is too large".

The evidence/explanation fields (`automation_reason`, `residual_human_value`,
`note`, `evidence`) exist to satisfy the design doc's guardrails 5 and 6:
explain every depreciation, and explain every residual-value claim. No silent
verdict — if a capability is marked low residual value, the reason must travel
with it.
"""

from pydantic import BaseModel, Field, field_validator


def _coerce_str_list(value):
    """Coerce a model-provided value into a list[str].

    The LLM occasionally returns a joined string ("Banking → Insurance") for a
    field the schema declares as an array, despite prompt instructions. This
    validator splits such a string on the delimiters the model favours (arrows,
    commas, semicolons, newlines) so Pydantic validation passes. A list or any
    other value passes through unchanged (Pydantic's own list[str] coercion then
    handles it). This is defensive, not a prompt substitute — the prompt still
    asks for arrays.
    """
    if isinstance(value, str):
        import re

        parts = re.split(r"\s*[→,;]\s*|\n", value)
        return [p.strip() for p in parts if p.strip()]
    return value

# The fields of each stage, as the model must answer them. Listed in module
# constants rather than reconstructed from the Pydantic model because the
# `json_schema()` below mirrors them by hand, and the two must move together.
# A name here that the model class does not carry (or vice versa) is a defect.

# allow-hardcode: the target shape of the model's answer, not configuration.
_HISTORY_FIELDS = (
    "roles",
    "industries",
    "functions",
    "systems",
    "trajectory",
)

# allow-hardcode: as above.
_AUTOMATION_FIELDS = (
    "assessments",
    "scarce_capabilities",
)

# allow-hardcode: as above.
_BENCHMARK_FIELDS = (
    "work_family",
    "current_work",
    "current_required",
    "declining",
    "emerging",
    "scarce",
    "automation_summary",
)

# allow-hardcode: as above.
_GAP_FIELDS = (
    "gaps",
    "evidence_gaps",
)

# allow-hardcode: as above.
_RESIDUAL_FIELDS = (
    "historical_strength",
    "automation_exposure",
    "current_relevance",
    "scarce_capabilities",
    "depreciated_capabilities",
    "emerging_capabilities",
    "evidence_gaps",
    "overall_assessment",
    "current_profile",
)

# The scalar (single-string) fields across every stage. Named once so the
# hand-built schema and the Pydantic model agree on which fields are strings
# vs arrays vs nested objects. `confidence`/numbers are handled separately.
# allow-hardcode: the scalar field names, not configuration.
_SCALAR_FIELDS = {
    # history role / work item
    "period",
    "title",
    "domain",
    "seniority",
    "scope",
    "evidence",
    "task",
    "tool",
    "judgment_level",
    "accountability",
    # history rollup
    "trajectory",
    # automation assessment
    "capability",
    "automation_level",
    "automation_reason",
    "residual_human_value",
    # benchmark
    "work_family",
    "automation_summary",
    # gap entry
    "status",
    "note",
    # residual
    "historical_strength",
    "automation_exposure",
    "current_relevance",
    "overall_assessment",
    "current_profile",
}


class WorkItem(BaseModel):
    """One decomposed piece of work a role actually involved (design doc §5).

    Job titles are not sufficient — a "Commercial Underwriter" collects risk
    information, assesses financials, applies rules, configures a policy. The
    engine decomposes each role into the actual work performed, because
    automation exposure is assessed against the *work*, not the title.
    """

    task: str = ""
    tool: str = ""
    judgment_level: str = ""
    accountability: str = ""


class HistoryRole(BaseModel):
    """One role from the candidate's history, with its work decomposed.

    Combines design doc Layer 1 (fact extraction — the role's period, title,
    domain, seniority, scope) with Layer 2 (work decomposition — the `work`
    list). Deliberately value-neutral: this states what the person did, not
    what it is worth today.
    """

    period: str = ""
    title: str = ""
    domain: str = ""
    seniority: str = ""
    scope: str = ""
    work: list[WorkItem] = Field(default_factory=list)
    evidence: str = ""


class HistoryProfile(BaseModel):
    """Pass 1 — the candidate's history, value-neutral (design doc §4).

    Layer 1 (facts) + Layer 2 (work decomposition) rolled up. `roles` carries
    the decomposed work per role; `industries`/`functions`/`systems` are the
    flat rollups; `trajectory` is the ordered arc of domains the candidate
    moved through. This layer is deliberately value-neutral — it states what
    the person did, not what it is worth today.
    """

    roles: list[HistoryRole] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list)
    trajectory: list[str] = Field(default_factory=list)

    # The model sometimes returns a joined string for these flat lists despite
    # the prompt asking for arrays; coerce defensively.
    _c_industries = field_validator("industries", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_functions = field_validator("functions", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_systems = field_validator("systems", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_trajectory = field_validator("trajectory", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )


class AutomationAssessment(BaseModel):
    """One capability assessed for automation exposure + residual human value.

    Combines Layer 3 (automation test) and Layer 4 (human scarcity). The
    `automation_level` is one of the five levels from the design doc §6 table
    (very_high / high / medium / low / very_low). `residual_human_value` is the
    part of the work that still requires a human — the scarce capability. Both
    `automation_reason` and `residual_human_value` are mandatory prose because
    guardrails 5 and 6 forbid a silent verdict: if work is depreciated, the
    reason must travel with it.
    """

    capability: str = ""
    automation_level: str = ""
    automation_reason: str = ""
    residual_human_value: str = ""


class AutomationProfile(BaseModel):
    """Pass 2 — automation exposure across the candidate's capabilities.

    Layer 3 + Layer 4 rollup. `assessments` is per-capability; the
    `scarce_capabilities` list is the cross-cutting set of capabilities the
    candidate holds that remain difficult to commoditize (design doc §7).
    """

    assessments: list[AutomationAssessment] = Field(default_factory=list)
    scarce_capabilities: list[str] = Field(default_factory=list)

    _c_scarce = field_validator("scarce_capabilities", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )


class MarketBenchmark(BaseModel):
    """Pass 3 — today's version of the work family (design doc §8 + §9).

    Layer 5 (current market benchmark) + Layer 6 (depreciation/appreciation).
    The benchmark represents *today's* version of the work, not the historical
    version — what the market requires now, what is declining, what is
    emerging, what is scarce. A 10-year hotline operator cannot automatically
    receive a "10-year experience" premium against a benchmark where tier-1 is
    now an AI voice agent.
    """

    work_family: str = ""
    current_work: list[str] = Field(default_factory=list)
    current_required: list[str] = Field(default_factory=list)
    declining: list[str] = Field(default_factory=list)
    emerging: list[str] = Field(default_factory=list)
    scarce: list[str] = Field(default_factory=list)
    automation_summary: str = ""

    _c_work = field_validator("current_work", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_req = field_validator("current_required", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_declining = field_validator("declining", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_emerging = field_validator("emerging", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_scarce_bm = field_validator("scarce", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )


class CapabilityGap(BaseModel):
    """One capability assessed against today's standard (design doc §6 gap).

    The `status` is the load-bearing 5-way distinction from design doc §2:

      demonstrated | partially_demonstrated | claimed_weak | not_evidenced |
      contradicted

    "Not evidenced" is NOT "does not possess" (guardrail 4) — the prompt
    enforces this distinction. `note` explains the status so the verdict is
    never silent.
    """

    capability: str = ""
    status: str = ""
    note: str = ""


class GapAnalysis(BaseModel):
    """Pass 4 — gaps between the candidate and today's standard (design doc §9).

    `gaps` is the per-capability assessment against the benchmark's required /
    emerging / scarce capabilities; `evidence_gaps` is the list of specific
    things the CV does not evidence but a recruiter could verify.
    """

    gaps: list[CapabilityGap] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)

    _c_evidence_gaps = field_validator("evidence_gaps", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )


class ResidualValueAssessment(BaseModel):
    """Pass 5 — the decomposable residual value + candid profile (doc §10/§11).

    Layer 7 (residual value) + Layer 8 (current candidate profile). This is the
    headline output. It must NOT reduce to a single opaque score (doc §10); it
    is a decomposable assessment where every claim traces to evidence or a
    benchmark. `current_profile` is the candid paragraph (doc §11) that
    describes who the candidate is in *today's* market — not "10+ years
    experience", but what remains scarce and economically useful.
    """

    historical_strength: str = ""
    automation_exposure: str = ""
    current_relevance: str = ""
    scarce_capabilities: list[str] = Field(default_factory=list)
    depreciated_capabilities: list[str] = Field(default_factory=list)
    emerging_capabilities: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    overall_assessment: str = ""
    current_profile: str = ""

    _c_scarce_rv = field_validator("scarce_capabilities", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_depreciated = field_validator("depreciated_capabilities", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_emerging_rv = field_validator("emerging_capabilities", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )
    _c_evidence_gaps_rv = field_validator("evidence_gaps", mode="before")(
        classmethod(lambda cls, v: _coerce_str_list(v))
    )


class CandidateIntelligenceResult(BaseModel):
    """All five stages, as the API returns and the row stores them.

    The container the worker persists and the API serializes. Each field is one
    pipeline stage's structured output.
    """

    history: HistoryProfile
    automation: AutomationProfile
    benchmark: MarketBenchmark
    gaps: GapAnalysis
    residual: ResidualValueAssessment


def json_schema() -> dict:
    """The schema sent to the model as prompt text.

    Hand-built to satisfy strict structured output (`additionalProperties: false`
    and `required` naming every property), and written as a flat, readable
    object a model can hold in one pass. One stage at a time is requested —
    never all five in one call — so each stage's schema is returned by its own
    module calling the matching helper below.
    """
    return {
        "history": _history_schema(),
        "automation": _automation_schema(),
        "benchmark": _benchmark_schema(),
        "gaps": _gaps_schema(),
        "residual": _residual_schema(),
    }


def _work_item_schema() -> dict:
    """The nested work-item object inside a history role."""
    properties: dict[str, object] = {name: {"type": "string"} for name in _WORK_ITEM_FIELDS}
    return {
        "type": "object",
        "properties": properties,
        "required": list(_WORK_ITEM_FIELDS),
        "additionalProperties": False,
    }


def _history_role_schema() -> dict:
    """The nested history-role object, with its work-item list."""
    scalar_role_fields = [f for f in _HISTORY_ROLE_FIELDS if f != "work"]
    properties: dict[str, object] = {name: {"type": "string"} for name in scalar_role_fields}
    properties["work"] = {
        "type": "array",
        "items": _work_item_schema(),
    }
    required = list(_HISTORY_ROLE_FIELDS)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _history_schema() -> dict:
    """The history stage schema, with its nested role list + flat rollups."""
    properties: dict[str, object] = {
        "roles": {
            "type": "array",
            "items": _history_role_schema(),
        },
    }
    for name in _HISTORY_FIELDS:
        if name == "roles":
            continue
        properties[name] = (
            {"type": "string"}
            if name in _SCALAR_FIELDS
            else {"type": "array", "items": {"type": "string"}}
        )
    required = list(_HISTORY_FIELDS)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _automation_schema() -> dict:
    """The automation stage schema, with its nested assessment entries."""
    entry_properties: dict[str, object] = {
        name: {"type": "string"} for name in _AUTOMATION_ASSESSMENT_FIELDS
    }
    return {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": entry_properties,
                    "required": list(_AUTOMATION_ASSESSMENT_FIELDS),
                    "additionalProperties": False,
                },
            },
            "scarce_capabilities": {"type": "array", "items": {"type": "string"}},
        },
        "required": list(_AUTOMATION_FIELDS),
        "additionalProperties": False,
    }


def _benchmark_schema() -> dict:
    """The market-benchmark stage schema — scalar fields + array rollups."""
    properties: dict[str, object] = {}
    for name in _BENCHMARK_FIELDS:
        properties[name] = (
            {"type": "string"}
            if name in _SCALAR_FIELDS
            else {"type": "array", "items": {"type": "string"}}
        )
    return {
        "type": "object",
        "properties": properties,
        "required": list(_BENCHMARK_FIELDS),
        "additionalProperties": False,
    }


def _gaps_schema() -> dict:
    """The gap-analysis stage schema, with its nested capability-gap entries."""
    entry_properties: dict[str, object] = {
        name: {"type": "string"} for name in _CAPABILITY_GAP_FIELDS
    }
    return {
        "type": "object",
        "properties": {
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": entry_properties,
                    "required": list(_CAPABILITY_GAP_FIELDS),
                    "additionalProperties": False,
                },
            },
            "evidence_gaps": {"type": "array", "items": {"type": "string"}},
        },
        "required": list(_GAP_FIELDS),
        "additionalProperties": False,
    }


def _residual_schema() -> dict:
    """The residual-value stage schema — the decomposable assessment + profile."""
    properties: dict[str, object] = {}
    for name in _RESIDUAL_FIELDS:
        properties[name] = (
            {"type": "string"}
            if name in _SCALAR_FIELDS
            else {"type": "array", "items": {"type": "string"}}
        )
    return {
        "type": "object",
        "properties": properties,
        "required": list(_RESIDUAL_FIELDS),
        "additionalProperties": False,
    }


# The fields of a nested history role (Layer 1 + Layer 2 combined).
# allow-hardcode: the nested-object field shape, not configuration.
_HISTORY_ROLE_FIELDS = (
    "period",
    "title",
    "domain",
    "seniority",
    "scope",
    "work",
    "evidence",
)

# The fields of a decomposed work item (Layer 2).
# allow-hardcode: as above.
_WORK_ITEM_FIELDS = (
    "task",
    "tool",
    "judgment_level",
    "accountability",
)

# The fields of an automation assessment entry (Layer 3 + Layer 4).
# allow-hardcode: as above.
_AUTOMATION_ASSESSMENT_FIELDS = (
    "capability",
    "automation_level",
    "automation_reason",
    "residual_human_value",
)

# The fields of a capability-gap entry (Layer 6 gap).
# allow-hardcode: as above.
_CAPABILITY_GAP_FIELDS = (
    "capability",
    "status",
    "note",
)
