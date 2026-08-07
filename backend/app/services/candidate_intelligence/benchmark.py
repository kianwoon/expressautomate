"""Pass 3 — today's version of the work family (doc L5 + L6 depreciation).

The reference layer. Reads the candidate's history and produces the *current*
market benchmark for their work family — what the work looks like today, what
it now requires, and which capabilities are declining, emerging, or scarce.

This is the critical reference (doc §8). The benchmark represents today's
version of the work, not the historical version. A 10-year hotline operator
cannot automatically receive a "10-year experience" premium when tier-1 is now
an AI voice/chat agent; the benchmark names that shift so the gap and residual
stages reason against today, not against the candidate's start date.

Combines design doc Layer 5 (current benchmark) and Layer 6
(depreciation/appreciation). Layer 6 classifies capabilities as depreciating
(losing economic value — data entry, routine reporting, standard
reconciliation), stable/conditional (domain knowledge, regulatory knowledge,
exception handling), or appreciating (AI-enabled workflows, automation design,
AI governance, transformation). The benchmark carries these as the `declining`
/ `emerging` / `scarce` lists.

The benchmark knowledge comes from the model's world knowledge of current
technology and operating models (no separate market database), matching how
the Job Intelligence engine infers its outputs today. This keeps the benchmark
current with the model's training, and avoids a separate data-maintenance
burden.

Fed the history (not the automation result) because the benchmark is about the
work family, independent of this specific candidate's exposure — the gap stage
joins the two.

Mirrors `job_intelligence/understand.py` in shape.
"""

from app.core.config import settings
from app.services.candidate_intelligence.history import model
from app.services.candidate_intelligence.input import CandidateContext
from app.services.candidate_intelligence.render import history_text
from app.services.candidate_intelligence.schema import MarketBenchmark, json_schema
from app.services.llm.client import LLMResult, complete_json

# allow-hardcode: a prompt, not configuration.
PROMPT = """You are a labour-market analyst. Given this candidate's work history, \
describe what their WORK FAMILY looks like TODAY — the current standard for \
this kind of work, not the historical version.

This benchmark is the reference the candidate is measured against. A decade of \
experience in a role is not worth a decade of current-market value when the \
work itself has been reshaped by technology and new operating models.

Rules:
- `work_family`: a short name for the work family this candidate's history \
sits in (e.g. "Commercial insurance underwriting", "Customer hotline \
operations", "Reinsurance administration").
- `current_work`: what the work actually looks like TODAY — the tasks and \
activities the role now involves, after technology and AI have reshaped it. \
(e.g. for a hotline: "AI voice/chat agent handles tier-1; human handles \
complex escalation, AI-agent supervision, knowledge/workflow design".)
- `current_required`: the capabilities the market now requires for this work \
family — what a hiring manager would ask for today, including modern \
capabilities (AI-enabled workflows, automation design) where relevant.
- `declining`: capabilities in this work family that are losing economic \
value — routinised, commoditised, or largely automated (e.g. "manual data \
entry", "routine reconciliation", "scripted troubleshooting").
- `emerging`: capabilities that are gaining value in this work family — the \
appreciating skills the market now rewards (e.g. "AI-agent supervision", \
"automation design", "business-rule design", "transformation").
- `scarce`: the capabilities that remain genuinely hard to find and hard to \
automate in this work family — the human capabilities that still command a \
premium.
- `automation_summary`: a candid paragraph on how automation and AI have \
changed this work family overall.
- Be candid and current. Do NOT assume every job disappears because AI can \
perform some tasks (no technology worship) — but DO name the shifts that have \
genuinely happened. The benchmark must reflect today, not five years ago.
- Ground the `work_family` in the candidate's actual history; ground the \
benchmark content in your knowledge of current technology and operating \
models for that work family.

Return JSON matching this schema:
{schema}

THE CANDIDATE'S HISTORY:
{history}
"""


def build_prompt(context: CandidateContext, history_as_text: str) -> str:
    """Separate from `infer_benchmark` so a prompt change is testable without a model.

    `context` is accepted for signature symmetry with the other stages even
    though this prompt reads only the history — the candidate's own CV is not
    the reference, the work family's current state is.
    """
    return PROMPT.format(
        schema=json_schema()["benchmark"],
        history=history_as_text,
    )


async def infer_benchmark(
    context: CandidateContext, history, *, llm=None
) -> tuple[MarketBenchmark, LLMResult]:
    """Produce the current market benchmark for the candidate's work family."""
    resolve = llm or complete_json
    prompt = build_prompt(context, history_text(history))
    result = await resolve(
        prompt,
        model=model(),
        schema=None,
        base_url=settings.CEREBRAS_BASE_URL,
        api_key=settings.CEREBRAS_API_KEY,
        extra_body={
            "max_tokens": settings.EXTRACTION_MAX_TOKENS,
            "reasoning_effort": settings.EXTRACTION_REASONING_EFFORT_FAST,
        },
    )
    return MarketBenchmark.model_validate(result.data), result
