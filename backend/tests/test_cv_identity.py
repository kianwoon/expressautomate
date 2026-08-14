"""Reading a person's contact details off a CV so it can create its own candidate.

The properties that matter are the same three the career extractor keeps, said
about three fields instead of a career:

- A quotation that is really on the page survives.
- A quotation that is not on the page is dropped to absence, not repaired and
  not shown to a recruiter.
- A field the CV does not state is never invented.

Identity is email-or-phone (`candidate_matching`); a name is only a label. So
the one property unique to this extractor — `has_matchable_identity` — is what
the caller uses to decide between creating a candidate and holding the CV for
review.

No test here reaches a model. `complete_json` is replaced by the spy below, and
the autouse fixture supplies the settings rather than trusting the ambient
environment — CI has no `.env`, and a test that depends on one is testing the
configuration.

allow-hardcode: the model ids, CV text and model responses below are fixtures.
"""

import pytest

from app.core.config import settings
from app.services.cv.identity import (
    extract_identity,
    identity_json_schema,
)
from app.services.ingest.schema import NOT_MENTIONED
from app.services.llm.client import LLMInvalidJSON, LLMResult

CV = (
    "Evelyn Tan\n"
    "evelyn.tan@example.com\n"
    "+65 9123 4567\n"
    "Senior Recruiter at KLN Logistics.\n"
)


@pytest.fixture(autouse=True)
def _configured_extraction(monkeypatch):
    """Every test gets its own model. Nothing here ever calls one."""
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")
    monkeypatch.setattr(settings, "EXTRACTION_MAX_TOKENS", 4096)


def _field(quote: str, value: str | None = None) -> dict:
    at = CV.find(quote)
    return {
        "value": value if value is not None else quote,
        "evidence": quote,
        "start_char": at,
        "end_char": at + len(quote),
        "confidence": 0.9,
    }


def _missing() -> dict:
    return {"value": NOT_MENTIONED, "confidence": 0.0}


def _payload(**overrides) -> dict:
    payload = {
        "full_name": _field("Evelyn Tan"),
        "email": _field("evelyn.tan@example.com"),
        "phone": _field("+65 9123 4567"),
    }
    payload.update(overrides)
    return payload


# A quotation that is nowhere on the page. The model sounds certain; the page
# is what gives it away.
_FABRICATED_EMAIL = {
    "value": "e.tan@fake-domain.test",
    "evidence": "e.tan@fake-domain.test",
    "start_char": 0,
    "end_char": 21,
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


async def test_contact_fields_quoting_the_page_survive():
    llm = _Spy(_payload())

    identity, result = await extract_identity(CV, llm=llm)

    assert llm.models == [settings.EXTRACTION_MODEL_FAST], "one pass, three fields"
    assert result.model == settings.EXTRACTION_MODEL_FAST
    assert identity.full_name.value == "Evelyn Tan"
    assert identity.email.value == "evelyn.tan@example.com"
    assert identity.phone.value == "+65 9123 4567"
    assert identity.has_matchable_identity


async def test_a_fabricated_email_is_dropped_to_absence():
    """A value whose quote is not on the page is discarded, not repaired.

    The whole point of identity extraction is a key to match a candidate on. A
    fabricated email would create a phantom person or merge two real ones, so a
    quote that fails verification becomes None rather than a guess.
    """
    llm = _Spy(_payload(email=_FABRICATED_EMAIL))

    identity, _ = await extract_identity(CV, llm=llm)

    assert identity.email is None
    # The honest fields survive — name is a display label, phone still matches.
    assert identity.full_name.value == "Evelyn Tan"
    assert identity.phone is not None
    assert identity.has_matchable_identity, "phone alone is still a matchable identity"


async def test_a_field_the_cv_does_not_state_is_not_invented():
    llm = _Spy(_payload(email=_missing()))

    identity, _ = await extract_identity(CV, llm=llm)

    assert identity.email is None
    assert identity.phone is not None


async def test_a_cv_with_no_email_and_no_phone_has_no_matchable_identity():
    """The caller uses this to decide: hold for review, do not invent a person."""
    llm = _Spy(_payload(email=_missing(), phone=_missing()))

    identity, _ = await extract_identity(CV, llm=llm)

    assert identity.email is None
    assert identity.phone is None
    assert not identity.has_matchable_identity
    # A name alone is still returned — it is a display label, not nothing.
    assert identity.full_name.value == "Evelyn Tan"


async def test_a_fabricated_name_is_dropped_but_identity_remains_matchable():
    """Name is display-only; a bad name must not cost the matchable keys."""
    fabricated_name = {
        "value": "John Smith",
        "evidence": "John Smith",
        "start_char": 0,
        "end_char": 10,
        "confidence": 0.99,
    }
    llm = _Spy(_payload(full_name=fabricated_name))

    identity, _ = await extract_identity(CV, llm=llm)

    assert identity.full_name is None
    assert identity.email is not None
    assert identity.phone is not None
    assert identity.has_matchable_identity


async def test_an_unusable_model_answer_raises():
    llm = _Spy(LLMInvalidJSON("no parseable answer"))

    with pytest.raises(LLMInvalidJSON):
        await extract_identity(CV, llm=llm)


async def test_extraction_never_asks_a_provider_to_compile_the_schema():
    """Same scar as career/email extraction: the schema travels as prompt text."""
    llm = _Spy(_payload())

    await extract_identity(CV, llm=llm)

    assert llm.calls[0]["schema"] is None
    assert "full_name" in llm.calls[0]["prompt"]
    assert NOT_MENTIONED in llm.calls[0]["prompt"]
    assert CV in llm.calls[0]["prompt"]


async def test_extraction_goes_to_deepseek_with_a_configured_budget():
    llm = _Spy(_payload())

    await extract_identity(CV, llm=llm)

    assert llm.calls[0]["base_url"] == settings.LLM_PROVIDER_BASE_URL
    assert llm.calls[0]["api_key"] == settings.LLM_PROVIDER_API_KEY
    assert llm.calls[0]["model"] == settings.EXTRACTION_MODEL_FAST
    assert llm.calls[0]["extra_body"]["max_tokens"] == settings.EXTRACTION_MAX_TOKENS


def test_the_schema_names_every_key_it_expects_back():
    schema = identity_json_schema()

    assert set(schema["properties"]) == {"full_name", "email", "phone"}
    assert schema["additionalProperties"] is False
    for field in schema["properties"].values():
        # Each property nullable so absence is an explicit object, not a silence.
        assert set(field["required"]) == set(field["properties"])


def test_a_value_that_quotes_nothing_is_rejected():
    """The anti-fabrication validator, inherited from ExtractedField, still bites."""
    from app.services.cv.identity import IdentityResult

    with pytest.raises(ValueError):
        IdentityResult.model_validate(
            {"email": {"value": "evelyn.tan@example.com", "confidence": 0.9}}
        )
