"""Why this candidate fits this job order, said only where the CV agrees (§15).

The shape is the one CV and email extraction use — two passes, quotations
verified against the source, a stronger attempt only on evidence that the first
fell short — but none of their code: those pipelines validate a CV or a vacancy
schema and wrap their prompts around a document. What carries over is the
discipline, not the parser.

Three things are stricter here than in either of them, because the sentence
this module produces is *about a person* and is read as a reason to put them in
front of a client.

**A quotation that is not on the candidate's page costs the whole
explanation.** Extraction keeps the two-thirds of a CV it could prove and drops
the rest; there is no equivalent partial credit for a reason. A recruiter reads
"strong Boolean search background" as a claim, and half of it being sourced
does not make the other half less invented. So the candidate keeps their
deterministic score and gets no prose at all.

**A candidate with no parsed document is a note, not an omission.** The text
verified against is the extracted CV stored at `CandidateDocument.text_key`
precisely so a span stays checkable after the fact. Someone with no such
document has nothing to check against, which is a fact about our records rather
than about them — and a recruiter who sees eight explanations and two blanks
must be able to tell "we could not read their CV" from "the model had nothing
to say".

**The prompt never carries a protected-attribute code, in any field.** Every
piece of opportunity text the prompt assembles — title, description *and*
requirements — goes through `redact` first, because a coded requirement can sit
in any of them and stripping one while passing the others through would look
like a guard while being none. `redact` only catches *coded* discrimination, so
the prompt also tells the model to ignore any requirement about a protected
characteristic and to say that it saw one; that report is returned here and
stored on the run in Task 6. A model told to notice something whose noticing
goes nowhere is a comment, not a safeguard.

`_attempts()` is imported rather than re-derived. It is pure configuration —
the fast model at low effort, then the strong model at high effort — and a
second copy would drift from it.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.services.ingest.evidence import verify
from app.services.ingest.extract import _attempts
from app.services.ingest.schema import ExtractedField
from app.services.llm.client import LLMInvalidJSON, complete_json
from app.services.sourcing.redact import redact

log = get_logger(__name__)

# allow-hardcode: notes shown to a recruiter, not configuration.
NO_DOCUMENT_NOTE = (
    "No parsed CV on file, so no reason could be checked against a source."
)


@dataclass
class MatchCandidate:
    """One ranked candidate, as the sourcing run already has them in hand.

    `cv_text` is the extracted text of the candidate's document — `None` when
    there is no parsed document. `score` is carried through untouched; this
    module never writes to it, and the fact that an explanation was refused
    must not read as a worse fit.

    **This is a whitelist, and that is the whole point.** It names the five
    things a model may see about a person; it is not built from a `Candidate`
    row, so a column added to that table does not appear here by accident.
    `candidates.sex`, `.race`, `.race_detail`, `.nationality` and
    `.date_of_birth` are therefore absent and must stay absent — they are
    recorded because a MOM form asks for them, and handing one to the model
    that explains why somebody fits is exactly the laundering `redact.py`
    exists to prevent. `education_years` and a candidate's languages are held
    to the same rule here: they are eligibility facts for a job order to state,
    not material for a free-text justification.

    Adding a field to this dataclass adds it to the prompt. Do not do it for
    any of the above. `tests/test_candidate_demographics_api.py` fails if one
    of them reaches `build_prompt`'s output.
    """

    candidate_id: Any
    full_name: str | None = None
    current_title: str | None = None
    skills: list[str] = field(default_factory=list)
    score: Any = None
    cv_text: str | None = None


@dataclass(frozen=True)
class Explanation:
    """What may be shown beside a candidate, and why there is nothing to show.

    Exactly one of `reason` and `note` carries meaning: a supported explanation
    has prose and located offsets, and everything else has an empty reason and
    a note saying what stopped it.
    """

    candidate_id: Any
    reason: str = ""
    evidence: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    confidence: float = 0.0
    note: str | None = None


@dataclass(frozen=True)
class ProtectedReport:
    """What the run must record about discriminatory requirements.

    `redacted_codes` is what this code removed before the model ever saw it;
    `requirements` is what the model says it noticed anyway and refused to act
    on. The two are different evidence — one proves the guard fired, the other
    catches the plain-words case no glossary code can match.
    """

    noticed: bool = False
    requirements: list[str] = field(default_factory=list)
    redacted_codes: list[str] = field(default_factory=list)


# allow-hardcode: a prompt, not configuration.
PROMPT = """Say why each candidate below fits this job order.

Rules:
- One entry in `explanations` per candidate you can justify, keyed by the
  `candidate_id` exactly as given. A candidate you cannot justify from their CV
  text is simply left out — never pad the list.
- `reason` is one or two sentences a recruiter can read, about this job.
- `evidence` must be text copied VERBATIM from THAT candidate's CV text below —
  character for character, with nothing added, shortened or paraphrased. It is
  checked against their CV, and an explanation whose quote is not found there is
  discarded entirely, so quoting loosely loses the whole explanation.
- `confidence` is how sure you are, between 0 and 1. Answer honestly: a low
  number costs nothing, an overstated one is what gets an invented reason shown
  to a client.
- Never compare candidates to each other, and never rank them. Each one is
  judged against the job order alone.
- IGNORE any requirement concerning a protected characteristic — race,
  nationality, ethnicity, gender, age, religion, marital or family status,
  disability, or pregnancy. Never use one as a reason, for or against anyone.
  List every such requirement you noticed, quoted from the job order, in
  `protected_requirements` so a human can review it. That list is a report, not
  a filter: an empty list means you saw none.

Return JSON matching this schema:
{schema}

Job order:
Title: {title}
Description: {description}
Requirements: {requirements}

Candidates:
{candidates}
"""


def json_schema() -> dict:
    """The shape asked for, sent as prompt text rather than a response format.

    Same reason as extraction: not every provider compiles a grammar, and a
    request rejected for an unsupported `response_format` fails identically
    every time. The parsing below is the real enforcement.
    """
    return {
        "type": "object",
        "properties": {
            "protected_requirements": {"type": "array", "items": {"type": "string"}},
            "explanations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "reason": {"type": "string"},
                        "evidence": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "candidate_id",
                        "reason",
                        "evidence",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["protected_requirements", "explanations"],
        "additionalProperties": False,
    }


def _redacted_opportunity(opportunity, codes) -> tuple[dict[str, str], list[str]]:
    """Every field the prompt carries, cleaned, plus what was taken out of it.

    All three fields go through `redact` — not the requirements alone. A code
    lands wherever the recruiter typed it, and the extracted title carries the
    shorthand as often as the requirements do.
    """
    fields = {
        "title": getattr(opportunity, "job_title", None) or "",
        "description": getattr(opportunity, "job_description", None) or "",
        "requirements": getattr(opportunity, "requirements", None) or "",
    }
    cleaned: dict[str, str] = {}
    removed: list[str] = []
    for name, value in fields.items():
        cleaned[name], hits = redact(value, list(codes or []))
        for code in hits:
            if code not in removed:
                removed.append(code)
    return cleaned, removed


def _candidate_block(candidate: MatchCandidate) -> str:
    """One candidate as the model sees them: who they are and their CV text."""
    skills = ", ".join(candidate.skills or []) or "Not recorded"
    return (
        f"candidate_id: {candidate.candidate_id}\n"
        f"Name: {candidate.full_name or 'Not recorded'}\n"
        f"Current title: {candidate.current_title or 'Not recorded'}\n"
        f"Skills: {skills}\n"
        f"CV text:\n{candidate.cv_text}\n"
    )


def build_prompt(opportunity, candidates, codes=()) -> tuple[str, list[str]]:
    """Separate from `explain_matches` so a prompt change is testable without a model."""
    cleaned, removed = _redacted_opportunity(opportunity, codes)
    return (
        PROMPT.format(
            schema=json.dumps(json_schema()),
            title=cleaned["title"] or "Not mentioned",
            description=cleaned["description"] or "Not mentioned",
            requirements=cleaned["requirements"] or "Not mentioned",
            candidates="\n---\n".join(_candidate_block(c) for c in candidates),
        ),
        removed,
    )


def _top(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    """The highest-scoring N, N from settings.

    Sorted here rather than trusted from the caller, so the promise that only
    the best N are sent to a model holds whatever order Task 6 hands over. A
    candidate whose score is `None` — nothing was comparable — sorts last
    rather than crashing the comparison.
    """
    ranked = sorted(
        candidates,
        key=lambda c: (c.score is not None, c.score if c.score is not None else 0),
        reverse=True,
    )
    return ranked[: settings.SOURCING_EXPLAIN_TOP_N]


def _supported(entry: dict, candidate: MatchCandidate) -> Explanation | None:
    """Turn one model answer into an explanation, or refuse it.

    The quote is wrapped in an `ExtractedField` because that is what `verify`
    takes, and `verify` *mutates* it: it locates the quote in the CV text and
    writes the real offsets back over the model's arithmetic. The offsets that
    come out of here therefore point at characters that exist. `value` is set to
    the quote itself rather than to the reason, because `verify` also asks
    whether the value follows from the quote, and prose about a candidate is not
    a normalisation of anything.
    """
    quote = (entry.get("evidence") or "").strip()
    reason = (entry.get("reason") or "").strip()
    if not quote or not reason:
        return None
    try:
        located = ExtractedField(value=quote, evidence=quote)
    except ValueError:
        return None
    if not verify(located, candidate.cv_text or ""):
        return None
    # verify() is vacuously True for a field whose value is "Not mentioned" —
    # it never locates anything, because there is nothing to locate. That is
    # correct for optional CV fields but wrong here: `quote` came from the
    # model's `evidence`, not a value it was allowed to leave blank, and a
    # candidate explanation with no located quote is exactly the invented
    # reason §15 exists to block. Reject anything verify() didn't actually
    # find a span for, whatever it returned.
    if located.is_missing or located.start_char is None:
        return None
    return Explanation(
        candidate_id=candidate.candidate_id,
        reason=reason,
        evidence=quote,
        start_char=located.start_char,
        end_char=located.end_char,
        confidence=float(entry.get("confidence") or 0.0),
    )


def _read(data, asked: dict) -> tuple[list[Explanation], list[str], bool]:
    """Read one model answer: what it supports, what it reported, and whether it fell short.

    "Fell short" is decided here by code, never by the model's opinion of its
    own work: an explanation whose quote is not on the candidate's page, or one
    the model itself is not confident in, is the only thing that buys a second,
    more expensive attempt (§32). An answer that simply covers fewer candidates
    is not short of anything — a model with nothing to say about someone is
    giving the right answer.
    """
    if not isinstance(data, dict):
        raise LLMInvalidJSON("explanation response was not an object")

    reported = [
        str(r).strip()
        for r in (data.get("protected_requirements") or [])
        if str(r).strip()
    ]

    kept: list[Explanation] = []
    seen_ids: set[str] = set()
    fell_short = False
    for entry in data.get("explanations") or []:
        if not isinstance(entry, dict):
            fell_short = True
            continue
        entry_id = str(entry.get("candidate_id"))
        candidate = asked.get(entry_id)
        if candidate is None:
            # An id we never sent. Nothing to verify it against, and inventing
            # the mapping would attach a reason to the wrong person.
            fell_short = True
            continue
        if entry_id in seen_ids:
            # Two entries naming the same candidate: the report promises one
            # row per candidate, so only the first entry for an id is even
            # considered — a rejected first entry does not hand the slot to
            # a later duplicate.
            continue
        seen_ids.add(entry_id)
        explanation = _supported(entry, candidate)
        if explanation is None:
            fell_short = True
            continue
        if explanation.confidence < settings.EXTRACTION_VERIFIED_CONFIDENCE:
            fell_short = True
        kept.append(explanation)

    return kept, reported, fell_short


async def explain_matches(
    opportunity,
    candidates: list[MatchCandidate],
    *,
    codes=(),
    llm=None,
) -> tuple[list[Explanation], ProtectedReport]:
    """Explain the top N matches, keeping only what their CVs support.

    Returns one `Explanation` per candidate that has either a supported reason
    or a note saying why it has none, and the protected-attribute report the
    run stores.

    `llm` defaults to None rather than to `complete_json` because a default
    argument binds the function object at definition time, and monkeypatching
    this module would then do nothing.
    """
    resolve = llm or complete_json
    top = _top(list(candidates))
    explainable = [c for c in top if (c.cv_text or "").strip()]
    notes = [
        Explanation(candidate_id=c.candidate_id, note=NO_DOCUMENT_NOTE)
        for c in top
        if not (c.cv_text or "").strip()
    ]

    prompt, removed = build_prompt(opportunity, explainable, codes)
    report = ProtectedReport(redacted_codes=removed)
    if not explainable:
        # Nobody has a source to check against, so there is no question to ask.
        # Paying for an answer we would be obliged to discard is the one case
        # where not calling the model is the whole of the behaviour.
        return notes, report

    asked = {str(c.candidate_id): c for c in explainable}
    kept: list[Explanation] = []
    reported: list[str] = []
    answered = False

    for model, effort in _attempts():
        try:
            result = await resolve(
                prompt,
                model=model,
                schema=None,
                base_url=settings.LLM_PROVIDER_BASE_URL,
                api_key=settings.LLM_PROVIDER_API_KEY,
                extra_body={
                    "max_tokens": settings.EXTRACTION_MAX_TOKENS,
                    "reasoning_effort": effort,
                },
            )
            kept, pass_reported, fell_short = _read(result.data, asked)
        except (LLMInvalidJSON, ValueError) as exc:
            log.warning("sourcing_explanation_unusable", model=model, error=repr(exc))
            continue

        # Union, never replace: the second pass answers the same job order
        # with a different model, not a clean slate, and a protected report
        # exists precisely to catch what redaction couldn't. A first pass that
        # noticed a plainly-worded requirement and a second that didn't must
        # not make that requirement disappear.
        for item in pass_reported:
            if item not in reported:
                reported.append(item)

        answered = True
        if not fell_short:
            break
        log.info("sourcing_explanation_escalating", model=model, kept=len(kept))

    if not answered:
        log.warning("sourcing_explanation_gave_up", candidates=len(explainable))
        kept = []

    # Only explanations still above the bar survive the second pass. Everything
    # dropped here leaves the candidate with their deterministic score, which is
    # the whole point: a number they earned, rather than prose we cannot source.
    kept = [e for e in kept if e.confidence >= settings.EXTRACTION_VERIFIED_CONFIDENCE]

    order = {str(c.candidate_id): i for i, c in enumerate(top)}
    explanations = sorted(
        kept + notes, key=lambda e: order.get(str(e.candidate_id), len(order))
    )
    return explanations, ProtectedReport(
        noticed=bool(reported),
        requirements=reported,
        redacted_codes=removed,
    )
