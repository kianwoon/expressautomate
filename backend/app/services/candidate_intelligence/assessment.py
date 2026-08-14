"""Pass 2 — the sharp synthesis (revised doc §15, §17, §20).

Reads the decomposed work from Pass 1 and produces the blunt assessment a
recruiter reads in 15 seconds. Lead with the conclusion, not the evidence.

This is NOT a summary. It is a verdict — decomposable, evidence-backed, but
blunt. No hedging, no "on the other hand", no corporate language. The revised
doc is explicit: "allowed to produce uncomfortable conclusions."

The three questions this pass answers:
1. What is this person's real work level, and what do they actually own?
2. After AI does the heavy lift, what scarce value remains?
3. If hired today, how fast could they contribute?
"""

from app.core.config import settings
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import work_text
from app.services.candidate_intelligence.schema import (
    CandidateAssessment,
    json_schema,
)
from app.services.candidate_intelligence.work import model
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a blunt, senior recruiter giving a hiring manager the honest read \
on this candidate — the kind of read you'd give a colleague in 30 seconds over \
coffee. No hedging. No LinkedIn copy. Lead with the conclusion.

The work decomposition below already stripped the CV's inflation and assessed \
each work unit's decision ownership, complexity, AI heavy-lift, and evidence. \
Your job is to synthesise that into a sharp assessment.

FORBIDDEN LANGUAGE (rewrite with the specific fact instead):
"solid", "strong", "valuable", "moderate", "well-rounded", "experienced \
professional", "good foundation", "demonstrates competence". These are the \
words of someone who hasn't read the CV. You have. Be specific.

FORBIDDEN: a single opaque verdict like "moderate candidate". Decompose: WHAT \
is scarce, WHAT is depreciated, WHAT is unproven.

`headline`: ONE sentence. The blunt bottom line. What kind of professional is \
this, at what real level, and is that level worth hiring? Examples of the \
tone: "A routine underwriting operator with a senior title, whose core work is \
now AI-compressed — hire only for speed on standard cases, not for judgment." \
Or: "A genuine reinsurance specialist with rare treaty-config depth — worth \
the premium IF the SICS expertise is real (verify it)." Lead with the truth.

`summary`: 3-4 sentences. What they actually do (operational? specialist? \
expert?), what they actually own (decisions? a process? nothing?), and what \
AI has done to the value of that work. No padding.

`work_level`: where the BULK of their demonstrated work sits — routine / \
operational / skilled / specialist / expert. Not where the titles suggest; \
where the WORK evidence puts them. A "Senior Executive" whose work units are \
all operational is operational. Say so.

`decision_authority`: the highest decision-ownership level the evidence \
actually supports (0-5, with the scale labels). If the CV never shows them \
owning a decision, say "Level 1-2 — executes and recommends; no evidence of \
owning decisions." Do not inflate.

`scarce_capabilities`: what remains genuinely scarce and economically useful, \
each with the evidence that proves it. If nothing is scarce, return an empty \
list and say so in the summary. Do not manufacture scarcity.

`depreciated_capabilities`: capabilities that have lost market value, each with \
the reason (what technology/automation/AI compressed it).

`unproven_claims`: the inflated claims a recruiter should probe in interview. \
Each has the CV's claim and the specific question to ask. "Not evidenced != \
does not possess" — these are verification items, not rejections.

`ai_exposure`: one blunt sentence on how much of their work AI can do the \
heavy lift of, and whether that means substitution (fewer humans needed) or \
compression (one human handles more volume).

`hire_readiness`: if hired today, how fast could they contribute, and on what \
work? Factor in the AI compression — if their role needs fewer people, say so.

`value_trajectory`: is their work getting more valuable (moving into scarce, \
modern capability) or less (accumulating years in increasingly commoditized \
work)? One sentence.

Ignore sex, race, nationality, age, religion, marital status.

Return JSON matching this schema:
{schema}

WORK DECOMPOSITION:
{work}
"""


def build_prompt(context: CandidateContext, work_as_text: str) -> str:
    return PROMPT.format(
        schema=json_schema()["assessment"],
        work=work_as_text,
    )


async def infer_assessment(
    context: CandidateContext, work, *, llm=None
) -> tuple[CandidateAssessment, LLMResult]:
    resolve = llm or complete_json
    prompt = build_prompt(context, work_text(work))
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
    return CandidateAssessment.model_validate(result.data), result
