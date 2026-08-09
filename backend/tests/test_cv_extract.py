"""Reading a career off a CV, and refusing to read one that is not there (§15).

The properties that matter here are the same three that matter for email, said
about a different document:

- A quotation that is really on the page survives.
- A quotation that is not on the page is not repaired, not rounded to the
  nearest plausible sentence, and not shown to a recruiter. It buys one more
  attempt from a model thinking harder, and if that attempt fails too the role
  is dropped rather than published.
- A date is reported at the precision the page actually carries. "Mar 2019" is
  a month. Inventing the 1st because a database column wants a full date is the
  fabrication §15 exists to prevent, and it is the kind that looks like data.

No test here reaches a model. `complete_json` is replaced by the spy below, and
the autouse fixture supplies the settings rather than trusting the ambient
environment — CI has no `.env`, and a test that depends on one is testing the
configuration.

allow-hardcode: the model ids, CV text and model responses below are fixtures.
"""

import pytest

from app.core.config import settings
from app.services.cv.extract import extract_cv
from app.services.cv.schema import CVResponse, ExtractedDate, cv_json_schema
from app.services.ingest.schema import NOT_MENTIONED
from app.services.llm.client import LLMInvalidJSON, LLMResult

CV = (
    "Evelyn Tan\n"
    "Senior Recruiter at KLN Logistics, Mar 2019 - Present.\n"
    "Skills: Boolean search, stakeholder management.\n"
)


@pytest.fixture(autouse=True)
def _configured_extraction(monkeypatch):
    """Every test gets its own models. Nothing here ever calls one."""
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_STRONG", "test/strong")
    monkeypatch.setattr(settings, "EXTRACTION_REASONING_EFFORT_FAST", "low")
    monkeypatch.setattr(settings, "EXTRACTION_REASONING_EFFORT_STRONG", "high")


def _field(quote: str, value: str | None = None, **extra) -> dict:
    at = CV.find(quote)
    return {
        "value": value if value is not None else quote,
        "evidence": quote,
        "start_char": at,
        "end_char": at + len(quote),
        "confidence": 0.9,
        **extra,
    }


def _missing() -> dict:
    return {"value": NOT_MENTIONED, "confidence": 0.0}


def _payload(**overrides) -> dict:
    role = {
        "title": _field("Senior Recruiter"),
        "company": _field("KLN Logistics"),
        "start_date": _field("Mar 2019", precision="month"),
        "end_date": _missing(),
        "summary": _missing(),
    }
    role.update(overrides)
    return {
        "roles": [role],
        "skills": [_field("Boolean search")],
    }


# A quotation that is nowhere on the page. The model sounds certain; the page
# is what gives it away.
_FABRICATED = {
    "value": "Head of Talent",
    "evidence": "Head of Talent at Global Search Partners",
    "start_char": 0,
    "end_char": 39,
    "confidence": 0.99,
}


_EMPTY_SALARY = {
    "last_drawn_salary": {
        "amount": None, "currency": None, "period": None,
        "evidence": None, "confidence": 0.0,
    },
    "expected_salary": {
        "amount": None, "currency": None, "period": None,
        "evidence": None, "confidence": 0.0,
    },
}


class _Spy:
    """Records the models it was asked for, and answers from a queue.

    `extract_cv` makes two calls: salary first, then career. The salary call is
    auto-answered with empty salary (no salary on the test CV) so tests that
    only care about roles/skills can pass career answers as before. Tests that
    need salary can inspect the salary call via `self.calls[0]`.
    """

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.models: list[str] = []
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, model, schema, **kw):
        self.models.append(model)
        self.calls.append({"model": model, "schema": schema, "prompt": prompt, **kw})
        if "salary" in prompt.lower() and "last_drawn_salary" in prompt:
            return LLMResult(data=_EMPTY_SALARY, model=model)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(data=answer, model=model)


async def test_a_role_quoting_the_page_survives():
    llm = _Spy(_payload())

    response, result = await extract_cv(CV, llm=llm)

    # Salary call (FAST) + career call (FAST) — one model, no escalation.
    assert llm.models == [
        settings.EXTRACTION_MODEL_FAST,
        settings.EXTRACTION_MODEL_FAST,
    ], "no reason to pay twice"
    assert result.model == settings.EXTRACTION_MODEL_FAST
    assert len(response.roles) == 1
    role = response.roles[0]
    assert role.title.value == "Senior Recruiter"
    # The offsets in the answer are the ones this codebase located, not the
    # ones the model counted.
    assert CV[role.title.start_char : role.title.end_char] == "Senior Recruiter"


async def test_a_role_quoting_something_absent_escalates():
    """Escalation is decided by a check we can make, not by self-reported 0.99."""
    llm = _Spy(_payload(title=_FABRICATED), _payload())

    response, result = await extract_cv(CV, llm=llm)

    # Salary call (FAST) + career calls (FAST → STRONG on escalation).
    assert llm.models == [
        settings.EXTRACTION_MODEL_FAST,
        settings.EXTRACTION_MODEL_FAST,
        settings.EXTRACTION_MODEL_STRONG,
    ]
    assert result.model == settings.EXTRACTION_MODEL_STRONG
    assert len(response.roles) == 1


async def test_evidence_that_fails_after_escalation_does_not_become_a_role():
    """The difference from email extraction, and it is deliberate.

    A vacancy that fails its checks is still shown to a recruiter as
    `needs_review`, because the email sits beside it. A fabricated employment
    history has no such safety net — it would read as a candidate's career.
    """
    llm = _Spy(_payload(title=_FABRICATED), _payload(title=_FABRICATED))

    response, result = await extract_cv(CV, llm=llm)

    assert result.model == settings.EXTRACTION_MODEL_STRONG
    assert response.roles == []


async def test_a_field_the_cv_does_not_state_is_not_invented():
    """A field the model fills in with no source text must be refused.

    A no-verification implementation would happily keep whatever the model
    wrote for `title` even though it quotes nothing on the page — this is the
    stub `test_a_field_the_cv_does_not_state_is_not_invented` used to pass
    against, because it only ever inspected the untouched `end_date`. Here the
    fabricated field is one that quotes text absent from the CV, which only a
    real verification pass discards; without it, this role would come back
    intact with an invented title.
    """
    llm = _Spy(_payload(title=_FABRICATED), _payload(title=_FABRICATED))

    response, _ = await extract_cv(CV, llm=llm)

    # Both attempts kept the fabricated title, so the whole role — the only
    # one the CV produced — must be dropped rather than published with it.
    assert response.roles == []


def test_a_month_date_never_acquires_a_day():
    """"Mar 2019" evidence must not license a `value` that carries a day.

    The stub this replaces asserted properties of an answer the model was
    simply never asked to give (`_payload()` never sends a day), so it would
    pass even against a schema with no precision check at all. This is a
    direct unit test of `ExtractedDate` rather than a round trip through
    `extract_cv`, and deliberately so: going through the full pipeline lets
    `verify`'s numeric corroboration (finding 1's note that it is "a safety
    net in the wrong layer") catch the fabricated "01" on its own, which would
    make this test pass even with the schema-level hole still open. Testing
    the schema directly isolates exactly what finding 1 is about — a day
    fabricated in `value` must not make `shape_precision` see a day, because
    only `evidence`, the quote off the page, may license precision.
    """
    with pytest.raises(ValueError):
        ExtractedDate.model_validate(
            {
                "value": "2019-03-01",
                "evidence": "Mar 2019",
                "confidence": 0.9,
                "precision": "day",
            }
        )


async def test_a_precision_finer_than_the_page_supports_is_refused():
    """"Mar 2019" cannot be a day, however the model labels it."""
    llm = _Spy(
        _payload(start_date=_field("Mar 2019", precision="day")),
        _payload(),
    )

    response, result = await extract_cv(CV, llm=llm)

    assert result.model == settings.EXTRACTION_MODEL_STRONG
    assert response.roles[0].start_date.precision == "month"


async def test_a_role_asserting_nothing_is_not_a_role():
    """Every field "Not mentioned" is an empty row, not a position held."""
    empty_role = {
        "title": _missing(),
        "company": _missing(),
        "start_date": _missing(),
        "end_date": _missing(),
        "summary": _missing(),
    }
    llm = _Spy(_payload() | {"roles": [empty_role], "skills": []})

    response, _ = await extract_cv(CV, llm=llm)

    assert response.roles == []


async def test_a_skill_the_cv_does_not_name_is_dropped():
    """"Not mentioned" in `skills` would otherwise pass `verify` vacuously."""
    llm = _Spy(_payload() | {"skills": [_missing()]})

    response, _ = await extract_cv(CV, llm=llm)

    assert response.skills == []


async def test_a_cv_the_model_cannot_read_raises_rather_than_inventing_a_career():
    llm = _Spy(LLMInvalidJSON("a"), LLMInvalidJSON("b"))

    with pytest.raises(LLMInvalidJSON):
        await extract_cv(CV, llm=llm)


async def test_extraction_never_asks_a_provider_to_compile_the_schema():
    """Same scar as email extraction: the schema travels as prompt text."""
    llm = _Spy(_payload())

    await extract_cv(CV, llm=llm)

    # calls[0] is the salary call; calls[1] is the career call.
    career_call = llm.calls[1]
    assert career_call["schema"] is None
    assert "roles" in career_call["prompt"]
    assert NOT_MENTIONED in career_call["prompt"]
    assert CV in career_call["prompt"]


async def test_extraction_goes_to_deepseek_with_a_configured_budget():
    llm = _Spy(_payload(title=_FABRICATED), _payload())

    await extract_cv(CV, llm=llm)

    assert llm.calls[0]["base_url"] == settings.DEEPSEEK_BASE_URL
    assert llm.calls[0]["api_key"] == settings.DEEPSEEK_API_KEY
    assert llm.calls[0]["extra_body"]["max_tokens"] == settings.EXTRACTION_MAX_TOKENS
    # Salary call (no reasoning_effort) + career calls (fast, strong).
    career_efforts = [
        c["extra_body"]["reasoning_effort"]
        for c in llm.calls
        if "reasoning_effort" in c["extra_body"]
    ]
    assert career_efforts == [
        settings.EXTRACTION_REASONING_EFFORT_FAST,
        settings.EXTRACTION_REASONING_EFFORT_STRONG,
    ]


def test_the_schema_names_every_key_it_expects_back():
    schema = cv_json_schema()

    assert set(schema["properties"]) == {
        "roles", "skills", "last_drawn_salary", "expected_salary",
    }
    role = schema["properties"]["roles"]["items"]
    assert set(role["required"]) == set(role["properties"])
    assert "precision" in role["properties"]["start_date"]["properties"]


def test_a_value_that_quotes_nothing_is_rejected():
    """The anti-fabrication validator, inherited and still strict."""
    with pytest.raises(ValueError):
        CVResponse.model_validate(
            {"roles": [{"title": {"value": "Head of Talent", "confidence": 0.9}}]}
        )


def test_the_prompt_instructs_salary_extraction():
    """Salary has its own focused prompt, separate from the career prompt."""
    from app.services.cv.extract import build_salary_prompt

    prompt = build_salary_prompt(CV)
    assert "last_drawn_salary" in prompt
    assert "expected_salary" in prompt
