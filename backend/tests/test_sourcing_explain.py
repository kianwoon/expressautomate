"""Why a candidate fits, and the refusal to say it when the CV does not agree.

Four properties are worth a test here, and three of them are refusals:

- a quotation that is really in the candidate's CV text survives and is shown;
- a quotation that is not in it yields no explanation at all — the candidate
  keeps the deterministic score, because an unsupported reason about a person
  is worse than none;
- a candidate with no parsed document is told apart from a candidate the model
  declined to explain: no CV text is a note, not a silence;
- a protected-attribute code never reaches the model, in *any* of the three
  fields the prompt carries, and what the model reports noticing comes back.

No test here reaches a model. `llm=` takes the spy below, and the autouse
fixture supplies the settings rather than trusting the ambient environment.

allow-hardcode: the model ids, CV text and model responses below are fixtures.
"""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.llm.client import LLMInvalidJSON, LLMResult
from app.services.sourcing.explain import (
    MatchCandidate,
    explain_matches,
)

CV = (
    "Evelyn Tan\n"
    "Senior Recruiter at KLN Logistics since Mar 2019.\n"
    "Led Boolean search training for the Singapore desk.\n"
)


@dataclass
class _Opportunity:
    job_title: str = "Senior Recruiter"
    job_description: str = "Recruit for the logistics desk."
    requirements: str = "Boolean search experience."


@dataclass
class _Code:
    code: str
    attribute: str | None


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Every test gets its own models and its own N. Nothing here calls one."""
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_STRONG", "test/strong")
    monkeypatch.setattr(settings, "EXTRACTION_REASONING_EFFORT_FAST", "low")
    monkeypatch.setattr(settings, "EXTRACTION_REASONING_EFFORT_STRONG", "high")
    monkeypatch.setattr(settings, "EXTRACTION_VERIFIED_CONFIDENCE", 0.8)
    monkeypatch.setattr(settings, "SOURCING_EXPLAIN_TOP_N", 2)


class _Spy:
    """Stands in for `complete_json`, recording every prompt it was sent."""

    def __init__(self, *payloads: dict):
        self.payloads = list(payloads)
        self.prompts: list[str] = []
        self.models: list[str] = []

    async def __call__(self, prompt, *, model, schema, **kwargs):
        self.prompts.append(prompt)
        self.models.append(model)
        payload = self.payloads.pop(0)
        if payload is None:
            raise LLMInvalidJSON("not json")
        return LLMResult(
            data=payload,
            model=model,
            raw=payload,
            latency_ms=1,
        )


def _candidate(cid="c1", *, text=CV, score="0.9"):
    return MatchCandidate(
        candidate_id=cid,
        full_name="Evelyn Tan",
        current_title="Senior Recruiter",
        skills=["Boolean search"],
        score=Decimal(score),
        cv_text=text,
    )


def _answer(quote: str, *, cid="c1", confidence=0.9, protected=()):
    return {
        "protected_requirements": list(protected),
        "explanations": [
            {
                "candidate_id": cid,
                "reason": "Runs Boolean search training.",
                "evidence": quote,
                "confidence": confidence,
            }
        ],
    }


async def test_a_quote_on_the_page_survives():
    spy = _Spy(_answer("Led Boolean search training"))
    explanations, report = await explain_matches(
        _Opportunity(), [_candidate()], llm=spy
    )

    assert len(explanations) == 1
    assert explanations[0].candidate_id == "c1"
    assert explanations[0].reason == "Runs Boolean search training."
    assert explanations[0].evidence == "Led Boolean search training"
    assert explanations[0].note is None
    # The offsets are the ones located in the CV, not the model's arithmetic.
    assert CV[explanations[0].start_char : explanations[0].end_char] == (
        "Led Boolean search training"
    )
    assert report.noticed is False
    # One pass: nothing failed, so nothing was escalated.
    assert spy.models == ["test/fast"]


async def test_a_quote_that_is_not_on_the_page_yields_no_explanation():
    invented = "Managed a team of forty across three offices"
    spy = _Spy(_answer(invented), _answer(invented))
    candidate = _candidate()
    explanations, _ = await explain_matches(_Opportunity(), [candidate], llm=spy)

    assert explanations == []
    # Both passes were made — an unsupported quote is what buys the second.
    assert spy.models == ["test/fast", "test/strong"]
    # The deterministic score is untouched: nothing here writes to it.
    assert candidate.score == Decimal("0.9")


async def test_low_confidence_escalates_then_keeps_the_better_answer():
    spy = _Spy(
        _answer("Led Boolean search training", confidence=0.2),
        _answer("Led Boolean search training", confidence=0.95),
    )
    explanations, _ = await explain_matches(_Opportunity(), [_candidate()], llm=spy)

    assert spy.models == ["test/fast", "test/strong"]
    assert len(explanations) == 1
    assert explanations[0].confidence == 0.95


async def test_a_candidate_with_no_cv_text_gets_a_note_not_a_silence():
    spy = _Spy(_answer("Led Boolean search training"))
    explanations, _ = await explain_matches(
        _Opportunity(),
        [_candidate(), _candidate(cid="c2", text=None, score="0.5")],
        llm=spy,
    )

    assert len(explanations) == 2
    blank = explanations[1]
    assert blank.candidate_id == "c2"
    assert blank.reason == ""
    assert blank.note
    # The model was never asked about someone there is nothing to check against.
    assert "c2" not in spy.prompts[0]


async def test_no_candidate_with_text_means_no_model_call_at_all():
    spy = _Spy()
    explanations, report = await explain_matches(
        _Opportunity(), [_candidate(text=None)], llm=spy
    )

    assert spy.prompts == []
    assert len(explanations) == 1
    assert explanations[0].note
    assert report.noticed is False


async def test_a_coded_requirement_never_reaches_the_prompt():
    codes = [_Code(code="C/F", attribute="race"), _Code(code="NS", attribute=None)]
    opportunity = _Opportunity(
        job_title="Senior Recruiter C/F",
        job_description="Desk lead, C/F only.",
        requirements="Boolean search. C/F preferred. NS trained.",
    )
    spy = _Spy(_answer("Led Boolean search training"))
    _, report = await explain_matches(
        opportunity, [_candidate()], codes=codes, llm=spy
    )

    # The assertion that matters: absent from the *whole* assembled prompt, not
    # from one field. A code stripped from the title while riding along in the
    # requirements would look like a guard while being none.
    assert "C/F" not in spy.prompts[0]
    # A code with no protected attribute is a real requirement and stays.
    assert "NS trained" in spy.prompts[0]
    assert report.redacted_codes == ["C/F"]


async def test_a_protected_requirement_the_model_noticed_comes_back():
    spy = _Spy(
        _answer(
            "Led Boolean search training",
            protected=["Asks for candidates under 35."],
        )
    )
    _, report = await explain_matches(_Opportunity(), [_candidate()], llm=spy)

    assert report.noticed is True
    assert report.requirements == ["Asks for candidates under 35."]


async def test_only_the_top_n_are_explained():
    ranked = [
        _candidate(cid=f"c{i}", score=str(round(0.9 - i / 10, 1))) for i in range(4)
    ]
    spy = _Spy(
        {
            "protected_requirements": [],
            "explanations": [
                {
                    "candidate_id": c.candidate_id,
                    "reason": "Runs Boolean search training.",
                    "evidence": "Led Boolean search training",
                    "confidence": 0.9,
                }
                for c in ranked[:2]
            ],
        }
    )
    explanations, _ = await explain_matches(_Opportunity(), ranked, llm=spy)

    # SOURCING_EXPLAIN_TOP_N is 2 in this suite.
    assert [e.candidate_id for e in explanations] == ["c0", "c1"]
    assert "c2" not in spy.prompts[0]


async def test_not_mentioned_evidence_yields_no_explanation():
    """§15: verify() is vacuously True for "Not mentioned" evidence, because
    there is nothing to locate. That must not read as support — a quote that
    was never located is not a quote at all. Fails against the pre-fix code,
    which called verify() and returned an Explanation with None offsets.
    """
    not_mentioned = _answer("Not mentioned")
    spy = _Spy(not_mentioned, not_mentioned)
    explanations, _ = await explain_matches(_Opportunity(), [_candidate()], llm=spy)

    assert explanations == []
    assert spy.models == ["test/fast", "test/strong"]


async def test_a_protected_report_from_the_first_pass_survives_a_silent_second():
    """A first pass that notices a plain-worded requirement must not be
    overwritten by a second pass that missed it — union, not replace."""
    spy = _Spy(
        _answer(
            "Managed a team of forty across three offices",  # unsupported: forces escalation
            protected=["Asks for candidates under 35."],
        ),
        _answer("Led Boolean search training"),  # second pass reports nothing
    )
    explanations, report = await explain_matches(
        _Opportunity(), [_candidate()], llm=spy
    )

    assert spy.models == ["test/fast", "test/strong"]
    assert len(explanations) == 1
    assert report.noticed is True
    assert report.requirements == ["Asks for candidates under 35."]


async def test_duplicate_candidate_ids_keep_only_the_first():
    payload = {
        "protected_requirements": [],
        "explanations": [
            {
                "candidate_id": "c1",
                "reason": "Runs Boolean search training.",
                "evidence": "Led Boolean search training",
                "confidence": 0.9,
            },
            {
                "candidate_id": "c1",
                "reason": "Second, contradicting entry.",
                "evidence": "Senior Recruiter at KLN Logistics",
                "confidence": 0.95,
            },
        ],
    }
    spy = _Spy(payload)
    explanations, _ = await explain_matches(_Opportunity(), [_candidate()], llm=spy)

    assert len(explanations) == 1
    assert explanations[0].reason == "Runs Boolean search training."


async def test_an_unparseable_answer_from_both_passes_explains_nobody():
    spy = _Spy(None, None)
    explanations, report = await explain_matches(
        _Opportunity(), [_candidate()], llm=spy
    )

    assert explanations == []
    assert report.noticed is False
    assert spy.models == ["test/fast", "test/strong"]
