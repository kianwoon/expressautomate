import pytest
from pydantic import ValidationError

from app.services.ingest.schema import (
    NOT_MENTIONED,
    ExtractedField,
    ExtractionResponse,
    json_schema,
)


def test_not_mentioned_is_a_value_not_a_null():
    """§15: 'the model found nothing' and 'we never asked' must stay distinct."""
    field = ExtractedField(
        value=NOT_MENTIONED, evidence=None, start_char=None, end_char=None, confidence=0.0
    )

    assert field.value == NOT_MENTIONED
    assert field.is_missing is True


def test_a_real_value_needs_a_quote_but_not_offsets():
    """Offsets stopped being part of the contract because models cannot count.

    A verbatim quotation with no offsets at all is a complete claim: evidence.py
    finds it in the email, and that is what §15 rests on. Requiring offsets here
    rejected correct extractions for arithmetic — see evidence.py's `locate`.
    """
    field = ExtractedField(
        value="SGD 6000", evidence="$6k", start_char=None, end_char=None, confidence=0.9
    )

    assert field.evidence == "$6k"


def test_multiple_jobs_parse():
    response = ExtractionResponse.model_validate(
        {
            "jobs": [
                {
                    "job_title": {
                        "value": "Finance Officer",
                        "evidence": "Finance officer",
                        "start_char": 10,
                        "end_char": 25,
                        "confidence": 0.95,
                    },
                    "salary": {
                        "value": "Up to 3500",
                        "evidence": "Up to $3500",
                        "start_char": 30,
                        "end_char": 41,
                        "confidence": 0.9,
                    },
                },
                {
                    "job_title": {
                        "value": "QA Executive",
                        "evidence": "QA Executive",
                        "start_char": 60,
                        "end_char": 72,
                        "confidence": 0.95,
                    },
                },
            ]
        }
    )

    assert len(response.jobs) == 2


def test_json_schema_declares_every_target_column():
    schema = json_schema()
    job = schema["properties"]["jobs"]["items"]["properties"]

    for name in (
        "company",
        "job_title",
        "job_description",
        "requirements",
        "salary",
        "salary_period",
        "working_hours",
        "work_arrangement",
        "employment_type",
        "duration",
        "location",
        "skills",
    ):
        assert name in job, f"{name} missing from the model-facing schema"


def test_a_value_that_quotes_nothing_is_rejected():
    """Offsets alone prove nothing.

    `value` is allowed to differ from the source ("Up to 3500" for
    "Up to $3,500"), so a slice can only ever be compared against the model's
    own quotation of it. Without `evidence`, a fabricated value could carry any
    two in-range integers and there would be nothing to check — the offsets
    would be decoration on an invention. This is the hole that made the
    no-fabrication rule (§15) a suggestion rather than a mechanism.
    """
    with pytest.raises(ValidationError):
        ExtractedField(value="Chief Executive", start_char=0, end_char=15)


def test_a_quotation_that_does_not_fit_its_own_span_is_accepted():
    """Offsets are a hint, so a hint disagreeing with the quote costs nothing.

    This used to be rejected as a self-contradiction, and that reading was
    wrong in practice: the quote was right and the span was the model's bad
    arithmetic, so the rule threw away good extractions. evidence.py locates
    the quote and overwrites the span with where it really is.
    """
    field = ExtractedField(
        value="SGD 6000", evidence="SGD 6,000", start_char=10, end_char=12
    )

    assert field.evidence == "SGD 6,000"


def test_the_model_is_asked_for_everything_the_parser_requires():
    """A schema laxer than the validator turns strictness into failed runs.

    Asking only for `value` let the model return a bare string it could not be
    held to; the parser then rejected the whole response for missing fields the
    model was never told to send. The two must agree.
    """
    field = json_schema()["properties"]["jobs"]["items"]["properties"]["salary"]

    # `confidence` joins them because strict mode requires every property, not
    # only the ones the parser insists on. The four the validator enforces are
    # what matters here; all five are what the provider demands.
    assert {"value", "evidence", "start_char", "end_char"} <= set(field["required"])
