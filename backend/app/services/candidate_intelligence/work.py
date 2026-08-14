"""Pass 1 — deep work decomposition (revised doc §3-§10).

Decomposes every role into concrete work units. For each work unit:
- What the CV CLAIMS (the candidate's own words, often inflated).
- What the work ACTUALLY IS (the operational verb, stripped of inflation).
- Decision ownership (0-5): the core dimension. Does not infer 3-5 without
  evidence.
- Complexity: operational / skilled / specialist / expert.
- AI heavy-lift: can AI do the heavy lift of this specific work unit?
- Evidence (A-E): and crucially, whether the claim is INFLATED — grand
  language over surface work.

Rule 1 (title blindness): the title is retained for chronology but never used
as evidence of complexity or seniority. Rule 2 (tenure): duration is not depth
— assess contribution maturity, not years.

This is the foundation pass. If the decomposition is shallow, every downstream
assessment inherits the shallowness. So the prompt forces depth: reason about
each work unit before producing JSON.
"""

from app.core.config import settings
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.schema import WorkAssessment, json_schema
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a sharp recruitment analyst who has read thousands of CVs and \
knows that CV language is MARKETING, not evidence. Your job is to decompose what \
this candidate ACTUALLY did — stripped of inflation — and assess each piece of \
work on its real substance.

TWO NON-NEGOTIABLE RULES:

RULE 1 — TITLE BLINDNESS. Job titles are weak metadata. "Senior Executive", \
"Specialist", "Manager", "Consultant", "Director" prove NOTHING about complexity \
or seniority. A senior title over routine work is common. Retain the title for \
chronology; never use it as evidence of capability.

RULE 2 — TENURE IS NOT DEPTH. Six months of employment is not six months of \
expertise. Assess what CONTRIBUTION LEVEL was reached (ramp-up → assisted → \
independent → ownership → expert → design), not how long they sat in the chair. \
A fast contributor who reached independent work in 3 months beats someone who \
stayed "assisted" for 2 years.

THE CLAIM-VS-SUBSTANCE TEST (THIS IS CRITICAL):

CVs inflate. A sentence that says "managed strategic reinsurance portfolios" \
might describe data entry into a system. A sentence that says "led cross-\
functional transformation" might mean attending coordination meetings. For \
every work unit, you must:

1. Record the CV's exact words in `claim`.
2. State the ACTUAL work underneath in `work` — the operational verb, stripped \
of grandeur. "Assessed risk" might really be "applied established underwriting \
rules to standard submissions". Say that.
3. If the claim sounds grander than the substance, set `inflated` to true and \
drop `evidence` to C (or lower). In `evidence_note`, say plainly: "Claims X, \
but the work described is Y." This is the single most useful signal a recruiter \
gets from this engine. Do not let inflated language pass as proven capability.

DECISION OWNERSHIP (0-5) — THE CORE DIMENSION:
  0 — No decision. Executes instructions.
  1 — Input. Provides information to someone deciding.
  2 — Recommendation. Analyses and recommends; someone else decides.
  3 — Decision. Makes the operational decision.
  4 — Ownership. Owns the policy/rule/process; accountable for outcome.
  5 — Design. Designs the decision framework, rules, model, or system.
Do NOT infer levels 3-5 without explicit evidence. "Prepared submissions" is \
1-2. "Owned the underwriting decision" is 3-4. "Designed the underwriting \
framework" is 5. If you cannot tell, use the lower level and mark evidence C/D.

COMPLEXITY:
  operational — repeatable, process-driven, established rules, limited judgment.
  skilled — requires training and consistent application, but follows known patterns.
  specialist — complex judgment, ambiguous cases, deep domain knowledge.
  expert — novel problems, creates methods, owns high-impact decisions.

AI HEAVY-LIFT — for each work unit, ask: can AI do the HEAVY LIFT (not "can AI \
replace the job")? If a human spent 8 hours and AI can now do 6.5 of those \
hours, the human's value is compressed even though they're still "required".
  ai_independent — AI cannot meaningfully perform this work.
  ai_assisted — AI helps, human does most substantive work.
  ai_heavy_lift — AI does most analysis/production; human reviews/directs.
  ai_dominant — AI does it end-to-end with limited human intervention.
  ai_agentic — AI executes the multi-step workflow autonomously.
`human_residual`: what valuable work remains for the human AFTER AI does the \
heavy lift? (review, exception judgement, accountability, relationship, etc.)

EVIDENCE LEVELS:
  A — Directly demonstrated by specific work described in the CV.
  B — Strongly supported by the pattern of work.
  C — Claimed but insufficiently demonstrated (inflated or thin).
  D — Not evidenced (the CV doesn't show it — NOT the same as "can't do it").
  E — Contradicted or internally inconsistent.

`tenure_months`: estimate from the period. `contribution_maturity`: where did \
they reach in the ramp-up → assisted → independent → ownership → expert → \
design arc, based on the work evidence (not the title or tenure alone)?

Also extract `education`: both formal (diplomas, degrees) and professional \
certifications (insurance modules, actuarial exams). Each entry: period, \
qualification, institution, field.

Every field described as a "list" or "array" MUST be a JSON array of separate \
elements, never a joined string.

Ignore sex, race, nationality, age, religion, marital status.

Return JSON matching this schema:
{schema}

VERIFIED FACTS:
{structured}

CV:
{cv}
"""


def build_prompt(context: CandidateContext) -> str:
    return PROMPT.format(
        schema=json_schema()["work"],
        structured=context.structured or "(none provided)",
        cv=context.cv_text or "(no CV text available)",
    )


def model() -> str:
    return settings.CANDIDATE_INTELLIGENCE_MODEL or settings.EXTRACTION_MODEL_FAST


async def infer_work(
    context: CandidateContext, *, llm=None
) -> tuple[WorkAssessment, LLMResult]:
    resolve = llm or complete_json
    prompt = build_prompt(context)
    result = await resolve(
        prompt,
        model=model(),
        schema=None,
        base_url=settings.LLM_PROVIDER_BASE_URL,
        api_key=settings.LLM_PROVIDER_API_KEY,
        extra_body={
            "max_tokens": settings.CANDIDATE_INTELLIGENCE_MAX_TOKENS,
            "reasoning_effort": settings.CANDIDATE_INTELLIGENCE_REASONING_EFFORT,
        },
    )
    return WorkAssessment.model_validate(result.data), result
