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


def test_a_real_value_requires_offsets():
    with pytest.raises(ValidationError):
        ExtractedField(
            value="SGD 6000", evidence="$6k", start_char=None, end_char=None, confidence=0.9
        )


def test_offsets_must_be_ordered():
    with pytest.raises(ValidationError):
        ExtractedField(value="x", evidence="x", start_char=50, end_char=10, confidence=0.9)


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
