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
from app.services.cv.schema import CVResponse, cv_json_schema
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
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
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


class _Spy:
    """Records the models it was asked for, and answers from a queue."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.models: list[str] = []
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, model, schema, **kw):
        self.models.append(model)
        self.calls.append({"model": model, "schema": schema, "prompt": prompt, **kw})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(data=answer, model=model)


async def test_a_role_quoting_the_page_survives():
    llm = _Spy(_payload())

    response, result = await extract_cv(CV, llm=llm)

    assert llm.models == [settings.EXTRACTION_MODEL_FAST], "no reason to pay twice"
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

    assert llm.models == [
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
    llm = _Spy(_payload())

    response, _ = await extract_cv(CV, llm=llm)

    role = response.roles[0]
    assert role.end_date.value == NOT_MENTIONED
    assert role.end_date.is_missing
    assert role.end_date.start_char is None


async def test_a_month_date_never_acquires_a_day():
    llm = _Spy(_payload())

    response, _ = await extract_cv(CV, llm=llm)

    start = response.roles[0].start_date
    assert start.value == "Mar 2019"
    assert start.precision == "month"
    # Nothing anywhere in the answer claims a day. A "1" appearing only inside
    # "2019" is why this looks at the whole serialised result rather than at
    # one field: a day invented in `value`, in `evidence` or in a second role
    # would all be equally wrong.
    assert "Mar 1" not in response.model_dump_json()
    assert "2019-03-01" not in response.model_dump_json()


async def test_a_precision_finer_than_the_page_supports_is_refused():
    """"Mar 2019" cannot be a day, however the model labels it."""
    llm = _Spy(
        _payload(start_date=_field("Mar 2019", precision="day")),
        _payload(),
    )

    response, result = await extract_cv(CV, llm=llm)

    assert result.model == settings.EXTRACTION_MODEL_STRONG
    assert response.roles[0].start_date.precision == "month"


async def test_a_cv_the_model_cannot_read_raises_rather_than_inventing_a_career():
    llm = _Spy(LLMInvalidJSON("a"), LLMInvalidJSON("b"))

    with pytest.raises(LLMInvalidJSON):
        await extract_cv(CV, llm=llm)


async def test_extraction_never_asks_a_provider_to_compile_the_schema():
    """Same scar as email extraction: the schema travels as prompt text."""
    llm = _Spy(_payload())

    await extract_cv(CV, llm=llm)

    assert llm.calls[0]["schema"] is None
    assert "roles" in llm.calls[0]["prompt"]
    assert NOT_MENTIONED in llm.calls[0]["prompt"]
    assert CV in llm.calls[0]["prompt"]


async def test_extraction_goes_to_cerebras_with_a_configured_budget():
    llm = _Spy(_payload(title=_FABRICATED), _payload())

    await extract_cv(CV, llm=llm)

    assert llm.calls[0]["base_url"] == settings.CEREBRAS_BASE_URL
    assert llm.calls[0]["api_key"] == settings.CEREBRAS_API_KEY
    assert llm.calls[0]["extra_body"]["max_tokens"] == settings.EXTRACTION_MAX_TOKENS
    assert [c["extra_body"]["reasoning_effort"] for c in llm.calls] == [
        settings.EXTRACTION_REASONING_EFFORT_FAST,
        settings.EXTRACTION_REASONING_EFFORT_STRONG,
    ]


def test_the_schema_names_every_key_it_expects_back():
    schema = cv_json_schema()

    assert set(schema["properties"]) == {"roles", "skills"}
    role = schema["properties"]["roles"]["items"]
    assert set(role["required"]) == set(role["properties"])
    assert "precision" in role["properties"]["start_date"]["properties"]


def test_a_value_that_quotes_nothing_is_rejected():
    """The anti-fabrication validator, inherited and still strict."""
    with pytest.raises(ValueError):
        CVResponse.model_validate(
            {"roles": [{"title": {"value": "Head of Talent", "confidence": 0.9}}]}
        )
