# Email Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn each stored recruitment email into verified `opportunities` rows — company, position, salary with period, hours, requirements, duration, location, received date — with every field traceable to a real span of the source.

**Architecture:** A cheap classifier decides whether an email is a job order at all. Recruitment emails go to an extraction call returning a strict JSON schema with character offsets; application code verifies each offset against the source before anything is trusted. A derived `quality_state` combines deterministic checks with model confidence. Human corrections live in a separate table so replaying a prompt upgrade can never overwrite them.

**Tech Stack:** OpenRouter via httpx, Pydantic schema validation, SQLAlchemy 2 async, Alembic, pytest.

**Spec:** [2026-07-27-email-ingestion-design.md](../specs/2026-07-27-email-ingestion-design.md)

**Prerequisite:** [the ingestion plan](2026-07-27-email-ingestion-pipeline.md) must be complete. This plan implements the `classify_email` and `extract_email` jobs it enqueues.

## Global Constraints

- **Nothing hardcoded.** Model names, base URLs, thresholds, and prompt versions come from `.env` via `settings`. A model id in source is a defect.
- **The AI must not fabricate** (§15). Missing information is `Not mentioned`, never a guess — and that is enforced by offset verification, not by asking nicely.
- **Every business table carries `tenant_id`** with a FORCE RLS policy in the same migration, or `verify_rls_enforced()` fails startup.
- **Never call a real model in tests.** Every LLM interaction goes through a fake.
- **Single file ≤ 1500 lines.**
- Run everything from `backend/`. Lint with `uv run ruff check .` before each commit.

## File Structure

| File | Responsibility |
|---|---|
| `app/models/opportunity.py` | `Opportunity` — the analytics-ready vacancy row |
| `app/models/extraction.py` | `Extraction`, `ExtractionEvidence`, `OpportunityFieldOverride` |
| `alembic/versions/*_extraction_tables.py` | Tables, indexes, RLS policies |
| `app/services/llm/client.py` | OpenRouter call, model routing, fake for tests |
| `app/services/ingest/preprocess.py` | HTML→text, signature and disclaimer trim |
| `app/services/ingest/classify.py` | Recruitment relevance gate |
| `app/services/ingest/schema.py` | The extraction JSON contract as Pydantic models |
| `app/services/ingest/extract.py` | Prompt build, model call, schema validation |
| `app/services/ingest/evidence.py` | Offset verification, `quality_state` derivation |
| `app/services/ingest/persist.py` | Write opportunities, extraction, evidence atomically |
| `app/services/retention.py` | Retention horizons and purge |
| `app/workers/settings.py` | (from the ingestion plan) the arq registry — every new job is registered here, never in `queue.py` |
| `tests/fixtures/emails/` | Real recruitment emails as golden files |

---

### Task 1: Extraction tables

**Files:**
- Create: `app/models/opportunity.py`, `app/models/extraction.py`
- Create: `alembic/versions/20260727_1900_extraction_tables.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_extraction_schema.py`

**Interfaces:**
- Consumes: `Base`, `UUIDPrimaryKey`, `TenantScoped`, `Timestamps`; `email_messages.id`
- Produces: `Opportunity`, `Extraction`, `ExtractionEvidence`, `OpportunityFieldOverride`

- [ ] **Step 1: Write the failing test**

`tests/test_extraction_schema.py`:

```python
import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session


@pytest.fixture
async def email_row(admin_session):
    tid, mid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " processing_status, source_state, classification_status)"
            " VALUES (:i, :t, :m, 'MSG', 'fetched', 'present', 'recruitment')"
        ),
        {"i": eid, "t": tid, "m": mid},
    )
    await admin_session.commit()
    yield tid, eid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


async def test_one_email_can_carry_several_opportunities(admin_session, email_row):
    """Plan §16: three vacancies in one email are three rows, not one."""
    tid, eid = email_row
    for title in ("Finance Officer", "Contract Accountant", "QA Executive"):
        await admin_session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, email_message_id,"
                " job_title_raw, review_status, quality_state)"
                " VALUES (:i, :t, :e, :title, 'ready', 'likely')"
            ),
            {"i": uuid.uuid4(), "t": tid, "e": eid, "title": title},
        )
    await admin_session.commit()

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM opportunities WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 3


async def test_salary_period_is_stored_alongside_the_amount(admin_session, email_row):
    """SGD 6,000 is meaningless for analytics without knowing per what."""
    tid, eid = email_row
    await admin_session.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, email_message_id, job_title_raw,"
            " salary_min, salary_max, salary_currency, salary_period, salary_raw,"
            " review_status, quality_state) VALUES (:i, :t, :e, 'Treasury', 5000, 7000,"
            " 'SGD', 'month', '$5,000-$7,000', 'ready', 'verified')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    row = (
        await admin_session.execute(
            text(
                "SELECT salary_period, salary_currency FROM opportunities"
                " WHERE email_message_id = :e"
            ),
            {"e": eid},
        )
    ).one()
    assert row.salary_period == "month"
    assert row.salary_currency == "SGD"


async def test_extractions_are_keyed_on_the_email_not_the_opportunity(
    admin_session, email_row
):
    """A run that finds nothing must still be recorded."""
    tid, eid = email_row
    await admin_session.execute(
        text(
            "INSERT INTO extractions (id, tenant_id, email_message_id, model_name,"
            " prompt_version, raw_response) VALUES (:i, :t, :e, 'test-model', 'v1', '{}')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM extractions WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 1


async def test_opportunities_are_tenant_isolated(admin_session, email_row):
    tid, eid = email_row
    await admin_session.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, email_message_id, job_title_raw,"
            " review_status, quality_state) VALUES (:i, :t, :e, 'Secret', 'ready', 'likely')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    async with tenant_session(uuid.uuid4()) as other:
        visible = (
            await other.execute(text("SELECT count(*) FROM opportunities"))
        ).scalar_one()
    assert visible == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_extraction_schema.py -v`
Expected: FAIL — `relation "opportunities" does not exist`

- [ ] **Step 3: Write the models**

`app/models/opportunity.py`:

```python
"""One vacancy (plan §16, §17, §25).

Analytics-ready from the first row. Retrofitting `salary_period` onto a year of
data means re-reading a year of emails; carrying a nullable column costs
nothing. `job_family` and `seniority` exist for the same reason and stay empty
until there is a controlled vocabulary — free-form model categories do not
aggregate, which is the opposite of the point.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Opportunity(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "opportunities"

    email_message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised from the email so listing and filtering never needs the join.
    received_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    company_name_raw: Mapped[str | None] = mapped_column(Text)
    company_name_normalized: Mapped[str | None] = mapped_column(Text, index=True)
    job_title_raw: Mapped[str | None] = mapped_column(Text)
    job_title_normalized: Mapped[str | None] = mapped_column(Text, index=True)
    job_family: Mapped[str | None] = mapped_column(String(64))
    seniority: Mapped[str | None] = mapped_column(String(32))

    job_description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    industry: Mapped[str | None] = mapped_column(String(128))

    employment_type: Mapped[str | None] = mapped_column(String(32))
    work_arrangement: Mapped[str | None] = mapped_column(String(32))
    working_hours_raw: Mapped[str | None] = mapped_column(Text)

    salary_min: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_period: Mapped[str | None] = mapped_column(String(16))
    salary_raw: Mapped[str | None] = mapped_column(Text)

    duration_raw: Mapped[str | None] = mapped_column(Text)
    duration_months: Mapped[int | None] = mapped_column(Integer)
    location_raw: Mapped[str | None] = mapped_column(Text)
    location_normalized: Mapped[str | None] = mapped_column(Text, index=True)

    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", index=True
    )
    quality_state: Mapped[str] = mapped_column(String(16), nullable=False, default="likely")
```

`app/models/extraction.py`:

```python
"""Extraction provenance (plan §14, §15).

`Extraction` is keyed on the email, not the opportunity: one model run may find
three vacancies or none, and the run that found none is exactly the one worth
inspecting later. Replay appends a row; nothing is updated in place, so an
email's extraction history is the ordered set of its rows.
"""

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Extraction(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "extractions"

    email_message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)


class ExtractionEvidence(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "extraction_evidence"

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_value: Mapped[str | None] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text)
    start_char: Mapped[int | None] = mapped_column(Integer)
    end_char: Mapped[int | None] = mapped_column(Integer)
    # Retained for calibration work, never shown to a user as a probability.
    model_confidence: Mapped[float | None] = mapped_column(Float)
    evidence_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class OpportunityFieldOverride(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A human correction. Replay must never overwrite one of these."""

    __tablename__ = "opportunity_field_overrides"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    ai_value: Mapped[str | None] = mapped_column(Text)
    human_value: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
```

Add all four to `app/models/__init__.py` and `__all__`.

- [ ] **Step 4: Write the migration**

```bash
uv run alembic revision --autogenerate -m "extraction tables"
```

Rename to `20260727_1900_extraction_tables.py` and append the same RLS block used
in the ingestion migration, for these four tables:

```python
PROTECTED = [
    ("opportunities", "tenant_id"),
    ("extractions", "tenant_id"),
    ("extraction_evidence", "tenant_id"),
    ("opportunity_field_overrides", "tenant_id"),
]
```

Then apply:

```bash
uv run alembic upgrade head
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_extraction_schema.py tests/test_rls.py -v`
Expected: PASS — 4 new tests, plus the RLS sweep still green.

- [ ] **Step 6: Commit**

```bash
git add app/models alembic/versions tests/test_extraction_schema.py
git commit -m "Add the opportunity and provenance tables"
```

---

### Task 2: Email preprocessing

**Files:**
- Create: `app/services/ingest/preprocess.py`
- Test: `tests/test_preprocess.py`

**Interfaces:**
- Consumes: nothing
- Produces: `def to_text(html: str, *, subject: str | None = None, sender: str | None = None) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_preprocess.py`:

```python
from app.services.ingest.preprocess import to_text


def test_html_tables_become_readable_lines():
    """Recruitment emails carry job details in tables constantly."""
    html = (
        "<table><tr><td>Salary</td><td>Up to $3500</td></tr>"
        "<tr><td>Location</td><td>Greenwich Drive</td></tr></table>"
    )

    result = to_text(html)

    assert "Salary" in result
    assert "Up to $3500" in result
    assert "Greenwich Drive" in result
    assert "<td>" not in result


def test_bullet_structure_survives():
    html = "<ul><li>Coordinate with finance</li><li>Prepare invoices</li></ul>"

    result = to_text(html)

    assert "Coordinate with finance" in result
    assert "Prepare invoices" in result
    assert result.count("\n") >= 1, "list items must not run together"


def test_subject_and_sender_are_prepended_as_context():
    result = to_text("<p>body</p>", subject="Finance officer", sender="e@x.com")

    assert result.startswith("SUBJECT: Finance officer")
    assert "SENDER: e@x.com" in result


def test_forwarded_content_is_kept():
    """Job orders arrive forwarded constantly; trimming them loses the job."""
    html = (
        "<p>FYI</p><div>From: client@example.com<br>"
        "We need a QA Executive, $3,700-$4,500</div>"
    )

    result = to_text(html)

    assert "QA Executive" in result
    assert "$3,700-$4,500" in result


def test_script_and_style_are_removed():
    html = "<style>.x{color:red}</style><script>alert(1)</script><p>Real content</p>"

    result = to_text(html)

    assert "Real content" in result
    assert "alert" not in result
    assert "color:red" not in result


def test_output_is_stable_for_offset_verification():
    """Evidence offsets index into this output, so it must be deterministic."""
    html = "<p>Up to $3500</p>"

    assert to_text(html) == to_text(html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preprocess.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ingest.preprocess'`

- [ ] **Step 3: Add the dependency**

```bash
uv add selectolax
```

- [ ] **Step 4: Write the preprocessor**

`app/services/ingest/preprocess.py`:

```python
"""HTML to model-ready text (plan §11).

Two properties matter more than prettiness:

1. **Deterministic.** Evidence offsets index into this output, so the same HTML
   must always produce byte-identical text or verification breaks.
2. **Conservative.** Forwarded chains and quoted replies are where the job
   order usually lives. Trimming them aggressively is how you lose the vacancy
   and never find out.
"""

from selectolax.parser import HTMLParser

_DROP_TAGS = ("script", "style", "head", "meta", "link")
_BLOCK_TAGS = ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table")


def to_text(html: str, *, subject: str | None = None, sender: str | None = None) -> str:
    """Flatten HTML to text, preserving line structure and table cells."""
    tree = HTMLParser(html or "")
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    for tag in _BLOCK_TAGS:
        for node in tree.css(tag):
            # A separator node keeps cells and list items from running together
            # into "SalaryUp to $3500", which no model reads correctly.
            node.insert_before(tree.create_tag("span"))

    body = tree.body or tree.root
    raw = body.text(separator="\n") if body is not None else ""

    lines = [line.strip() for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)

    header = []
    if subject:
        header.append(f"SUBJECT: {subject}")
    if sender:
        header.append(f"SENDER: {sender}")
    return "\n".join([*header, text]) if header else text
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_preprocess.py -v`
Expected: PASS — 6 tests. If `insert_before` with a created tag misbehaves in
your selectolax version, replace that loop with a pre-pass that rewrites block
tags to append `"\n"` to their text; the tests are the contract, not the
technique.

- [ ] **Step 6: Commit**

```bash
git add app/services/ingest/preprocess.py tests/test_preprocess.py pyproject.toml uv.lock
git commit -m "Flatten email HTML without losing forwarded job orders"
```

---

### Task 3: LLM client with model routing

**Files:**
- Create: `app/services/llm/__init__.py`, `app/services/llm/client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `settings`
- Produces:
  - `async def complete_json(prompt: str, *, model: str, schema: dict) -> LLMResult`
  - `class LLMResult` with `.data: dict`, `.model: str`, `.prompt_tokens`, `.completion_tokens`, `.latency_ms`, `.raw: dict`
  - `class FakeLLM` — queue of canned responses for tests
  - `class LLMInvalidJSON(Exception)`

- [ ] **Step 1: Write the failing test**

`tests/test_llm_client.py`:

```python
import httpx
import pytest

from app.services.llm.client import LLMInvalidJSON, complete_json


def _transport(payload, status=200):
    return httpx.MockTransport(lambda r: httpx.Response(status, json=payload))


async def test_returns_parsed_json_and_usage():
    payload = {
        "choices": [{"message": {"content": '{"jobs": []}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"jobs": []}
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 20
    assert result.latency_ms >= 0


async def test_non_json_content_raises_rather_than_guessing():
    payload = {
        "choices": [{"message": {"content": "Sure! Here are the jobs:"}}],
        "usage": {},
        "model": "test/fast",
    }

    with pytest.raises(LLMInvalidJSON):
        await complete_json(
            "prompt", model="test/fast", schema={}, transport=_transport(payload)
        )


async def test_json_wrapped_in_a_code_fence_is_recovered():
    """Models do this constantly; failing on it wastes a retry and a strong-model call."""
    payload = {
        "choices": [{"message": {"content": '```json\n{"jobs": []}\n```'}}],
        "usage": {},
        "model": "test/fast",
    }

    result = await complete_json(
        "prompt", model="test/fast", schema={}, transport=_transport(payload)
    )

    assert result.data == {"jobs": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.llm'`

- [ ] **Step 3: Add settings**

In `app/core/config.py`:

```python
    # --- AI extraction (plan §32) ---
    OPENROUTER_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_TIMEOUT_SECONDS: float = 90.0
    CLASSIFIER_MODEL: str = ""
    EXTRACTION_MODEL_FAST: str = ""
    EXTRACTION_MODEL_STRONG: str = ""
    PROMPT_VERSION: str = "v1"
```

In `.env`:

```bash
LLM_TIMEOUT_SECONDS=90
CLASSIFIER_MODEL=<a cheap model id from openrouter>
PROMPT_VERSION=v1
```

- [ ] **Step 4: Write the client**

`app/services/llm/__init__.py`: empty file.

`app/services/llm/client.py`:

```python
"""OpenRouter JSON completion.

Returns parsed data or raises. It never repairs a malformed response beyond
stripping a code fence, and never falls back to a default value — a silent
default here becomes a fabricated salary in someone's database.
"""

import json
import re
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMInvalidJSON(Exception):
    """The model did not return parseable JSON."""


@dataclass
class LLMResult:
    data: dict
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


async def complete_json(
    prompt: str,
    *,
    model: str,
    schema: dict,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LLMResult:
    started = time.monotonic()
    async with httpx.AsyncClient(
        base_url=settings.LLM_BASE_URL,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        transport=transport,
        headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
    ) as client:
        response = await client.post(
            "/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
        )
        response.raise_for_status()
        body = response.json()

    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage") or {}
    return LLMResult(
        data=_parse(content),
        model=body.get("model", model),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        latency_ms=int((time.monotonic() - started) * 1000),
        raw=body,
    )


def _parse(content: str) -> dict:
    if match := _FENCE.match(content or ""):
        content = match.group(1)
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMInvalidJSON(content[:500]) from exc
    if not isinstance(parsed, dict):
        raise LLMInvalidJSON(f"expected an object, got {type(parsed).__name__}")
    return parsed


class FakeLLM:
    """Test double. Queue responses; assert on the prompts it received."""

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, *, model: str, schema: dict, **_) -> LLMResult:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("FakeLLM ran out of queued responses")
        return LLMResult(data=self.responses.pop(0), model=model)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_llm_client.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/llm tests/test_llm_client.py app/core/config.py
git commit -m "Call OpenRouter for JSON without repairing bad output"
```

---

### Task 4: The relevance gate

**Files:**
- Create: `app/services/ingest/classify.py`
- Modify: `app/workers/jobs.py` (add `classify_email`), `app/workers/queue.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `complete_json`, `to_text`, `tenant_session`, `BodyStore`
- Produces:
  - `async def classify(text: str, llm=complete_json) -> Classification`
  - `class Classification` with `.status: str`, `.reason: str`, `.model: str`
  - `async def classify_email(ctx, email_message_id: str) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_classify.py`:

```python
import pytest

from app.services.ingest.classify import classify
from app.services.llm.client import FakeLLM, LLMInvalidJSON


async def test_a_job_order_is_recruitment():
    llm = FakeLLM({"is_job_order": True, "reason": "describes a vacancy"})

    result = await classify("We need a QA Executive, $3,700-$4,500", llm=llm)

    assert result.status == "recruitment"


async def test_an_invoice_is_not_recruitment():
    llm = FakeLLM({"is_job_order": False, "reason": "an invoice"})

    result = await classify("Invoice 4432 attached, payment due 30 days", llm=llm)

    assert result.status == "non_recruitment"


async def test_a_model_failure_fails_open_to_uncertain():
    """Failing closed loses a job order silently; failing open costs one call."""

    async def broken(prompt, **kwargs):
        raise LLMInvalidJSON("garbage")

    result = await classify("anything", llm=broken)

    assert result.status == "uncertain"


async def test_a_missing_key_fails_open_rather_than_defaulting_to_false():
    llm = FakeLLM({"reason": "model forgot the verdict field"})

    result = await classify("anything", llm=llm)

    assert result.status == "uncertain"


async def test_uncertain_still_proceeds_to_extraction():
    from app.services.ingest.classify import should_extract

    assert should_extract("recruitment") is True
    assert should_extract("uncertain") is True
    assert should_extract("non_recruitment") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ingest.classify'`

- [ ] **Step 3: Write the classifier**

`app/services/ingest/classify.py`:

```python
"""Recruitment relevance gate.

A recruiter's mailbox is mostly not job orders, and extracting from every
message costs money, storage, and privacy exposure for nothing. This gate is a
cheap model answering one yes/no question.

It fails **open**. An uncertain or broken classification proceeds to extraction:
the cost of a wasted extraction call is a fraction of a cent, and the cost of a
silently dropped job order is a vacancy the recruiter never sees and never
knows to look for.
"""

from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger
from app.services.llm.client import complete_json

log = get_logger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_order": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_job_order", "reason"],
}

PROMPT = """Decide whether this email is a recruitment job order — a message
describing one or more vacancies a recruiter is being asked to fill.

It IS a job order if it describes a role to hire for: a title, requirements,
salary, or a client asking for candidates.

It is NOT a job order if it is an invoice, a candidate's application or CV
submission, a meeting invite, a newsletter, or ordinary correspondence.

Return JSON: {{"is_job_order": true|false, "reason": "<one short sentence>"}}

EMAIL:
{email}
"""


@dataclass
class Classification:
    status: str
    reason: str
    model: str


def should_extract(status: str) -> bool:
    """Anything but a confident 'no' goes on to extraction."""
    return status != "non_recruitment"


async def classify(text: str, llm=None) -> Classification:
    """`llm` defaults to None, not to `complete_json`.

    A default argument binds at definition time, so `llm=complete_json` would
    capture the function object and make monkeypatching this module's
    `complete_json` do nothing — the test would pass through to a real HTTP
    call. Resolving at call time is what makes the fake reachable.
    """
    resolve = llm or complete_json
    model = settings.CLASSIFIER_MODEL
    try:
        result = await resolve(PROMPT.format(email=text), model=model, schema=SCHEMA)
        verdict = result.data.get("is_job_order")
        if not isinstance(verdict, bool):
            raise ValueError(f"missing is_job_order in {result.data!r}")
        return Classification(
            status="recruitment" if verdict else "non_recruitment",
            reason=str(result.data.get("reason", ""))[:500],
            model=result.model,
        )
    except Exception as exc:
        log.warning("classification_failed_open", error=repr(exc))
        return Classification(status="uncertain", reason=f"gate failed: {exc}"[:500],
                              model=model)
```

- [ ] **Step 4: Write the job**

Append to `app/workers/jobs.py`:

```python
async def classify_email(ctx, email_message_id: str) -> None:
    """Relevance gate (spec: Architecture)."""
    from app.services.ingest.classify import classify, should_extract
    from app.services.ingest.preprocess import to_text

    located = await _locate(email_message_id)
    if located is None:
        return
    tenant_id, mailbox_id, graph_message_id, status = located
    # `classifying` is accepted, not just `fetched`: a worker killed mid-classify
    # leaves the row at `classifying`, and rescan_stuck re-enqueues exactly this
    # job for it. Accepting only `fetched` would make that row retry forever.
    if status not in ("fetched", "classifying"):
        return

    await _set_status(tenant_id, email_message_id, "classifying")

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT body_html_r2_key, subject, sender_email, retention_until"
                    " FROM email_messages WHERE id = :i"
                ),
                {"i": email_message_id},
            )
        ).one()

    html = await body_store().get(row.body_html_r2_key) or ""
    body = to_text(html, subject=row.subject, sender=row.sender_email)
    verdict = await classify(body)

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE email_messages SET classification_status = :s,"
                " classification_reason = :r, classification_model = :m,"
                " classification_version = :v,"
                " processing_status = CASE WHEN :extract THEN 'classifying'"
                " ELSE 'skipped' END,"
                " retention_until = CASE WHEN :extract THEN retention_until"
                " ELSE now() + make_interval(days => :short) END"
                " WHERE id = :i"
            ),
            {
                "s": verdict.status,
                "r": verdict.reason,
                "m": verdict.model,
                "v": settings.PROMPT_VERSION,
                "extract": should_extract(verdict.status),
                "short": settings.NON_RECRUITMENT_RETENTION_DAYS,
                "i": email_message_id,
            },
        )

    if should_extract(verdict.status):
        await enqueue("extract_email", email_message_id=email_message_id)


async def _set_status(tenant_id: uuid.UUID, email_message_id: str, status: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE email_messages SET processing_status = :s WHERE id = :i"),
            {"s": status, "i": email_message_id},
        )
```

Add the setting to `app/core/config.py` and `.env`:

```python
    NON_RECRUITMENT_RETENTION_DAYS: int = 7
```

```bash
NON_RECRUITMENT_RETENTION_DAYS=7
```

Register `classify_email` in `WorkerSettings.functions` in
`app/workers/settings.py` — never in `queue.py`, which `jobs.py` imports from.

Add a test proving the resume path works, since this is the state
`rescan_stuck` drives:

```python
async def test_classify_resumes_a_row_left_at_classifying(monkeypatch, admin_session):
    """A worker killed mid-classify must be recoverable, not stuck forever."""
    # ... insert an email_messages row with processing_status = 'classifying' ...
    monkeypatch.setattr(classify_module, "complete_json",
                        FakeLLM({"is_job_order": True, "reason": "a vacancy"}))

    await jobs.classify_email({}, email_message_id=str(eid))

    status = (
        await admin_session.execute(
            text("SELECT processing_status FROM email_messages WHERE id = :i"),
            {"i": eid},
        )
    ).scalar_one()
    assert status != "classifying", "the row must have moved on"
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_classify.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/ingest/classify.py app/workers tests/test_classify.py app/core/config.py
git commit -m "Skip the mail that is not a job order"
```

---

### Task 5: The extraction contract

**Files:**
- Create: `app/services/ingest/schema.py`
- Test: `tests/test_extraction_contract.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `class ExtractedField(BaseModel)` — `value`, `evidence`, `start_char`, `end_char`, `confidence`
  - `class ExtractedJob(BaseModel)` — every opportunity field, each an `ExtractedField`
  - `class ExtractionResponse(BaseModel)` — `jobs: list[ExtractedJob]`
  - `NOT_MENTIONED: str`
  - `def json_schema() -> dict`

- [ ] **Step 1: Write the failing test**

`tests/test_extraction_contract.py`:

```python
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
    field = ExtractedField(value=NOT_MENTIONED, evidence=None, start_char=None,
                           end_char=None, confidence=0.0)

    assert field.value == NOT_MENTIONED
    assert field.is_missing is True


def test_a_real_value_requires_offsets():
    with pytest.raises(ValidationError):
        ExtractedField(value="SGD 6000", evidence="$6k", start_char=None,
                       end_char=None, confidence=0.9)


def test_offsets_must_be_ordered():
    with pytest.raises(ValidationError):
        ExtractedField(value="x", evidence="x", start_char=50, end_char=10,
                       confidence=0.9)


def test_multiple_jobs_parse():
    response = ExtractionResponse.model_validate(
        {
            "jobs": [
                {
                    "job_title": {"value": "Finance Officer", "evidence": "Finance officer",
                                  "start_char": 10, "end_char": 25, "confidence": 0.95},
                    "salary": {"value": "Up to 3500", "evidence": "Up to $3500",
                               "start_char": 30, "end_char": 41, "confidence": 0.9},
                },
                {
                    "job_title": {"value": "QA Executive", "evidence": "QA Executive",
                                  "start_char": 60, "end_char": 72, "confidence": 0.95},
                },
            ]
        }
    )

    assert len(response.jobs) == 2


def test_json_schema_declares_every_target_column():
    schema = json_schema()
    job = schema["properties"]["jobs"]["items"]["properties"]

    for name in (
        "company", "job_title", "job_description", "requirements", "salary",
        "salary_period", "working_hours", "work_arrangement", "employment_type",
        "duration", "location", "skills",
    ):
        assert name in job, f"{name} missing from the model-facing schema"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_extraction_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ingest.schema'`

- [ ] **Step 3: Write the contract**

`app/services/ingest/schema.py`:

```python
"""The model-facing extraction contract (plan §13, §15).

Every field carries its evidence *and* the character offsets that evidence came
from. Asking for evidence alone is not verification — a model can invent a
plausible quote as easily as a plausible salary. Offsets are checkable; prose
is not.
"""

from pydantic import BaseModel, Field, model_validator

NOT_MENTIONED = "Not mentioned"

FIELDS = (
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
)


class ExtractedField(BaseModel):
    value: str
    evidence: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    confidence: float = 0.0

    @property
    def is_missing(self) -> bool:
        return self.value.strip().lower() == NOT_MENTIONED.lower()

    @model_validator(mode="after")
    def _present_values_must_be_locatable(self) -> "ExtractedField":
        """A value with no offsets cannot be verified, so it is not accepted.

        This is the schema-level half of the no-fabrication rule; the other
        half is checking the offsets against the source in evidence.py.
        """
        if self.is_missing:
            return self
        if self.start_char is None or self.end_char is None:
            raise ValueError(f"{self.value!r} has no source offsets")
        if self.end_char <= self.start_char:
            raise ValueError(f"offsets out of order: {self.start_char}..{self.end_char}")
        return self


class ExtractedJob(BaseModel):
    company: ExtractedField | None = None
    job_title: ExtractedField | None = None
    job_description: ExtractedField | None = None
    requirements: ExtractedField | None = None
    salary: ExtractedField | None = None
    salary_period: ExtractedField | None = None
    working_hours: ExtractedField | None = None
    work_arrangement: ExtractedField | None = None
    employment_type: ExtractedField | None = None
    duration: ExtractedField | None = None
    location: ExtractedField | None = None
    skills: ExtractedField | None = None


class ExtractionResponse(BaseModel):
    jobs: list[ExtractedJob] = Field(default_factory=list)


def json_schema() -> dict:
    """Schema sent to the model. Derived, so it cannot drift from the parser."""
    field_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "evidence": {"type": "string"},
            "start_char": {"type": "integer"},
            "end_char": {"type": "integer"},
            "confidence": {"type": "number"},
        },
        "required": ["value"],
    }
    return {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": dict.fromkeys(FIELDS, field_schema),
                },
            }
        },
        "required": ["jobs"],
    }
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_extraction_contract.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/ingest/schema.py tests/test_extraction_contract.py
git commit -m "Require source offsets for every extracted value"
```

---

### Task 6: Evidence verification and quality state

**Files:**
- Create: `app/services/ingest/evidence.py`
- Test: `tests/test_evidence.py`

**Interfaces:**
- Consumes: `ExtractedField`, `ExtractedJob`
- Produces:
  - `def verify(field: ExtractedField, source: str) -> bool`
  - `def quality_state(job: ExtractedJob, source: str) -> str`
  - `def parse_salary(raw: str) -> tuple[float | None, float | None, str | None]`

- [ ] **Step 1: Write the failing test**

`tests/test_evidence.py`:

```python
from app.services.ingest.evidence import parse_salary, quality_state, verify
from app.services.ingest.schema import NOT_MENTIONED, ExtractedField, ExtractedJob

SOURCE = "Finance officer at KLN Logistics. Salary up to $3500 per month."


def _field(**kwargs):
    return ExtractedField(**kwargs)


def test_a_real_span_verifies():
    start = SOURCE.index("up to $3500")
    field = _field(value="3500", evidence="up to $3500", start_char=start,
                   end_char=start + len("up to $3500"), confidence=0.9)

    assert verify(field, SOURCE) is True


def test_a_fabricated_quote_fails_even_when_it_sounds_right():
    """The whole point: the model can invent the evidence, not the offsets."""
    field = _field(value="6000", evidence="salary is SGD 6,000", start_char=0,
                   end_char=19, confidence=0.98)

    assert verify(field, SOURCE) is False


def test_whitespace_differences_are_tolerated():
    start = SOURCE.index("up to $3500")
    field = _field(value="3500", evidence="up  to   $3500", start_char=start,
                   end_char=start + len("up to $3500"), confidence=0.9)

    assert verify(field, SOURCE) is True


def test_offsets_past_the_end_of_the_source_fail_rather_than_raise():
    field = _field(value="x", evidence="x", start_char=99_000, end_char=99_010,
                   confidence=0.9)

    assert verify(field, SOURCE) is False


def test_a_missing_field_is_not_a_verification_failure():
    field = _field(value=NOT_MENTIONED, confidence=0.0)

    assert verify(field, SOURCE) is True


def test_high_confidence_with_bad_evidence_is_not_verified():
    """Model confidence is not a calibrated probability and cannot outvote a
    failed deterministic check."""
    job = ExtractedJob(
        job_title=_field(value="Finance officer", evidence="Finance officer",
                         start_char=0, end_char=15, confidence=0.99),
        salary=_field(value="9999", evidence="salary is $9,999", start_char=0,
                      end_char=16, confidence=0.99),
    )

    assert quality_state(job, SOURCE) == "needs_review"


def test_everything_checking_out_is_verified():
    start = SOURCE.index("up to $3500")
    job = ExtractedJob(
        job_title=_field(value="Finance officer", evidence="Finance officer",
                         start_char=0, end_char=15, confidence=0.95),
        salary=_field(value="3500", evidence="up to $3500", start_char=start,
                      end_char=start + len("up to $3500"), confidence=0.95),
    )

    assert quality_state(job, SOURCE) == "verified"


def test_salary_parsing_extracts_a_range():
    assert parse_salary("$5,000-$7,000") == (5000.0, 7000.0, None)


def test_salary_parsing_extracts_a_currency_when_stated():
    assert parse_salary("SGD 6,000") == (6000.0, 6000.0, "SGD")


def test_unparseable_salary_returns_nothing_rather_than_a_guess():
    assert parse_salary("competitive") == (None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_evidence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ingest.evidence'`

- [ ] **Step 3: Write the verifier**

`app/services/ingest/evidence.py`:

```python
"""Deterministic verification of what the model claimed (plan §14, §15).

The rule the product depends on — "nothing invented" — is a string comparison
here, not an instruction in a prompt. A prompt can be ignored by a model. A
comparison against the source cannot.
"""

import re

from app.services.ingest.schema import ExtractedField, ExtractedJob

_WHITESPACE = re.compile(r"\s+")
_AMOUNT = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)")
_CURRENCY = re.compile(r"\b([A-Z]{3})\b")


def _normalise(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip().lower()


def verify(field: ExtractedField, source: str) -> bool:
    """Does the claimed span actually contain the claimed evidence?

    A missing field is vacuously valid — there is nothing to locate. Everything
    else must match the source at the offsets it named.
    """
    if field.is_missing:
        return True
    if field.start_char is None or field.end_char is None:
        return False
    if field.end_char > len(source):
        return False
    if not field.evidence:
        return False
    return _normalise(source[field.start_char : field.end_char]) == _normalise(
        field.evidence
    )


def parse_salary(raw: str) -> tuple[float | None, float | None, str | None]:
    """Pull a numeric range out of a salary string.

    Returns `(None, None, None)` rather than guessing when the string carries no
    figure — "competitive" is not a number, and inventing one here would defeat
    everything else in this module.
    """
    if not raw:
        return None, None, None
    amounts = [float(m.replace(",", "")) for m in _AMOUNT.findall(raw)]
    currency_match = _CURRENCY.search(raw)
    currency = currency_match.group(1) if currency_match else None
    if not amounts:
        return None, None, currency
    return min(amounts), max(amounts), currency


def quality_state(job: ExtractedJob, source: str) -> str:
    """Combine deterministic checks with the model's own confidence.

    Deterministic signals dominate on purpose: a self-reported 0.99 is not a
    calibrated probability, and must never outvote a span that does not exist.
    """
    fields = [f for f in vars(job).values() if isinstance(f, ExtractedField)]
    present = [f for f in fields if not f.is_missing]

    if not present:
        return "needs_review"
    if any(not verify(f, source) for f in present):
        return "needs_review"

    if job.salary is not None and not job.salary.is_missing:
        low, high, _ = parse_salary(job.salary.value)
        if low is None:
            return "needs_review"
        if job.salary_period is None or job.salary_period.is_missing:
            # An amount with no period is not comparable to any other amount,
            # which makes every salary analytic built on it wrong.
            return "likely"

    weakest = min(f.confidence for f in present)
    return "verified" if weakest >= 0.8 else "likely"
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_evidence.py -v`
Expected: PASS — 10 tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/ingest/evidence.py tests/test_evidence.py
git commit -m "Check every claimed span against the source"
```

---

### Task 7: Extraction and persistence

**Files:**
- Create: `app/services/ingest/extract.py`, `app/services/ingest/persist.py`
- Modify: `app/workers/jobs.py`, `app/workers/queue.py`
- Test: `tests/test_extract_job.py`

**Interfaces:**
- Consumes: everything above
- Produces:
  - `async def extract(source: str, *, llm=complete_json) -> tuple[ExtractionResponse, LLMResult]`
  - `async def persist(tenant_id, email_message_id, response, result, source) -> list[uuid.UUID]`
  - `async def extract_email(ctx, email_message_id: str) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_extract_job.py`:

```python
import uuid

import pytest
from sqlalchemy import text

from app.services.ingest.persist import persist
from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult

SOURCE = "Finance officer at KLN Logistics. Salary up to $3500 per month."


@pytest.fixture
async def email_row(admin_session):
    tid, mid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " received_datetime, processing_status, source_state, classification_status)"
            " VALUES (:i, :t, :m, 'MSG', now(), 'extracting', 'present', 'recruitment')"
        ),
        {"i": eid, "t": tid, "m": mid},
    )
    await admin_session.commit()
    yield tid, eid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


def _response(**overrides):
    start = SOURCE.index("up to $3500")
    payload = {
        "jobs": [
            {
                "job_title": {"value": "Finance officer", "evidence": "Finance officer",
                              "start_char": 0, "end_char": 15, "confidence": 0.95},
                "salary": {"value": "3500", "evidence": "up to $3500",
                           "start_char": start, "end_char": start + 11, "confidence": 0.9},
                "salary_period": {"value": "month", "evidence": "per month",
                                  "start_char": SOURCE.index("per month"),
                                  "end_char": SOURCE.index("per month") + 9,
                                  "confidence": 0.9},
            }
        ]
    }
    payload.update(overrides)
    return ExtractionResponse.model_validate(payload)


async def test_persist_writes_an_opportunity_with_a_parsed_salary(
    admin_session, email_row
):
    tid, eid = email_row
    result = LLMResult(data={}, model="test/fast")

    await persist(tid, eid, _response(), result, SOURCE)

    row = (
        await admin_session.execute(
            text(
                "SELECT job_title_raw, salary_min, salary_max, salary_period,"
                " quality_state, received_datetime FROM opportunities"
                " WHERE email_message_id = :e"
            ),
            {"e": eid},
        )
    ).one()
    assert row.job_title_raw == "Finance officer"
    assert float(row.salary_min) == 3500.0
    assert row.salary_period == "month"
    assert row.quality_state == "verified"
    assert row.received_datetime is not None, "denormalised for sorting"


async def test_persist_records_evidence_with_its_validity(admin_session, email_row):
    tid, eid = email_row

    await persist(tid, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE)

    rows = (
        await admin_session.execute(
            text(
                "SELECT field_name, evidence_valid FROM extraction_evidence ev"
                " JOIN extractions ex ON ex.id = ev.extraction_id"
                " WHERE ex.email_message_id = :e"
            ),
            {"e": eid},
        )
    ).all()
    assert {r.field_name for r in rows} >= {"job_title", "salary", "salary_period"}
    assert all(r.evidence_valid for r in rows)


async def test_a_run_that_finds_nothing_still_records_an_extraction(
    admin_session, email_row
):
    tid, eid = email_row

    ids = await persist(
        tid, eid, ExtractionResponse(jobs=[]), LLMResult(data={}, model="test/fast"), SOURCE
    )

    assert ids == []
    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM extractions WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 1, "the zero-vacancy run is the one worth inspecting"


async def test_replay_never_overwrites_a_human_correction(admin_session, email_row):
    """Criterion 5. A prompt upgrade must not undo a recruiter's fix."""
    tid, eid = email_row
    ids = await persist(tid, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE)

    await admin_session.execute(
        text(
            "INSERT INTO opportunity_field_overrides (id, tenant_id, opportunity_id,"
            " field_name, ai_value, human_value) VALUES (:i, :t, :o, 'location_raw',"
            " 'Maybank Tower', 'Raffles Place')"
        ),
        {"i": uuid.uuid4(), "t": tid, "o": ids[0]},
    )
    await admin_session.commit()

    await persist(tid, eid, _response(), LLMResult(data={}, model="test/strong"), SOURCE)

    override = (
        await admin_session.execute(
            text(
                "SELECT human_value FROM opportunity_field_overrides"
                " WHERE opportunity_id = :o"
            ),
            {"o": ids[0]},
        )
    ).scalar_one()
    assert override == "Raffles Place"


async def test_three_jobs_in_one_email_become_three_rows(admin_session, email_row):
    tid, eid = email_row
    one_job = _response().jobs[0].model_dump()
    response = ExtractionResponse.model_validate({"jobs": [one_job, one_job, one_job]})

    ids = await persist(tid, eid, response, LLMResult(data={}, model="test/fast"), SOURCE)

    assert len(ids) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_extract_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ingest.persist'`

- [ ] **Step 3: Write the extractor**

`app/services/ingest/extract.py`:

```python
"""Prompt construction and the extraction call (plan §12, §13, §32).

The prompt insists on offsets and on `Not mentioned`, but neither is trusted:
the schema rejects a value with no offsets, and evidence.py checks the offsets
against the source. The prompt only makes compliance likely; the code makes
non-compliance visible.
"""

from app.core.config import settings
from app.services.ingest.schema import NOT_MENTIONED, ExtractionResponse, json_schema
from app.services.llm.client import LLMInvalidJSON, LLMResult, complete_json

PROMPT = """Extract every job vacancy described in this email.

Rules:
- One entry in `jobs` per distinct vacancy. An email may describe several, or none.
- For each field, quote the exact text you took it from in `evidence`, and give
  `start_char` and `end_char` — the character offsets of that quote in the EMAIL
  text below. The quote must match the email exactly at those offsets.
- If the email does not state a field, set its value to "{not_mentioned}" and
  omit the offsets. Never infer, estimate, or fill in a typical value.
- `salary_period` is one of: hour, day, month, year. Extract it separately from
  the amount.
- `work_arrangement` is one of: onsite, hybrid, remote.

Return JSON matching this schema:
{schema}

EMAIL:
{email}
"""


async def extract(source: str, *, llm=None) -> tuple[ExtractionResponse, LLMResult]:
    """Extract, escalating to the strong model if the fast one cannot comply.

    Escalation is not a retry of the same thing: the fast model has already
    demonstrated it cannot produce this shape for this email, so repeating it
    would just cost another call (plan §32).
    """
    import json

    # Resolved at call time, not bound as a default — see the note in classify.py.
    resolve = llm or complete_json
    prompt = PROMPT.format(
        not_mentioned=NOT_MENTIONED,
        schema=json.dumps(json_schema()),
        email=source,
    )

    for model in (settings.EXTRACTION_MODEL_FAST, settings.EXTRACTION_MODEL_STRONG):
        try:
            result = await resolve(prompt, model=model, schema=json_schema())
            return ExtractionResponse.model_validate(result.data), result
        except (LLMInvalidJSON, ValueError):
            continue

    raise LLMInvalidJSON("neither model returned a valid extraction")
```

- [ ] **Step 4: Write the persistence layer**

`app/services/ingest/persist.py`:

```python
"""Write an extraction and its opportunities in one transaction (plan §14).

Append-only with respect to history: every run inserts a new `extractions` row
and new opportunities. Human corrections live in their own table and are never
touched here, which is what makes a prompt upgrade safe to replay across a
year of email.
"""

import uuid

from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.ingest.evidence import parse_salary, quality_state, verify
from app.services.ingest.schema import ExtractedField, ExtractedJob, ExtractionResponse
from app.services.llm.client import LLMResult

_SIMPLE = {
    "company": "company_name_raw",
    "job_title": "job_title_raw",
    "job_description": "job_description",
    "requirements": "requirements",
    "working_hours": "working_hours_raw",
    "work_arrangement": "work_arrangement",
    "employment_type": "employment_type",
    "duration": "duration_raw",
    "location": "location_raw",
}


def _value(field: ExtractedField | None) -> str | None:
    """`Not mentioned` becomes NULL in the column; the raw string lives on the
    evidence row, so the two cases stay distinguishable (plan §15)."""
    if field is None or field.is_missing:
        return None
    return field.value


async def persist(
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    response: ExtractionResponse,
    result: LLMResult,
    source: str,
) -> list[uuid.UUID]:
    from app.core.config import settings

    extraction_id = uuid.uuid4()
    opportunity_ids: list[uuid.UUID] = []

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO extractions
                    (id, tenant_id, email_message_id, model_name, prompt_version,
                     prompt_tokens, completion_tokens, latency_ms, raw_response)
                VALUES (:id, :t, :e, :model, :pv, :pt, :ct, :ms, :raw)
                """
            ),
            {
                "id": extraction_id,
                "t": tenant_id,
                "e": email_message_id,
                "model": result.model,
                "pv": settings.PROMPT_VERSION,
                "pt": result.prompt_tokens,
                "ct": result.completion_tokens,
                "ms": result.latency_ms,
                "raw": __import__("json").dumps(result.raw),
            },
        )

        for job in response.jobs:
            opportunity_id = uuid.uuid4()
            opportunity_ids.append(opportunity_id)
            await _insert_opportunity(session, tenant_id, email_message_id,
                                      opportunity_id, job, source)
            await _insert_evidence(session, tenant_id, extraction_id,
                                   opportunity_id, job, source)

    return opportunity_ids


async def _insert_opportunity(
    session,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    job: ExtractedJob,
    source: str,
) -> None:
    salary_min = salary_max = currency = None
    if job.salary is not None and not job.salary.is_missing:
        salary_min, salary_max, currency = parse_salary(job.salary.value)

    state = quality_state(job, source)
    params = {
        "id": opportunity_id,
        "t": tenant_id,
        "e": email_message_id,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency,
        "salary_period": _value(job.salary_period),
        "salary_raw": _value(job.salary),
        "skills": [s.strip() for s in (_value(job.skills) or "").split(",") if s.strip()],
        "quality": state,
        # Deterministic checks decide review, not the model's opinion of itself.
        "review": "needs_review" if state == "needs_review" else "ready",
    }
    params.update({column: _value(getattr(job, name)) for name, column in _SIMPLE.items()})

    columns = ", ".join(_SIMPLE.values())
    placeholders = ", ".join(f":{name}" for name in _SIMPLE)
    await session.execute(
        text(
            f"""
            INSERT INTO opportunities
                (id, tenant_id, email_message_id, received_datetime, {columns},
                 salary_min, salary_max, salary_currency, salary_period, salary_raw,
                 skills, quality_state, review_status)
            SELECT :id, :t, :e, em.received_datetime, {placeholders},
                   :salary_min, :salary_max, :currency, :salary_period, :salary_raw,
                   :skills, :quality, :review
            FROM email_messages em WHERE em.id = :e
            """
        ),
        {**params, **{name: params[column] for name, column in _SIMPLE.items()}},
    )


async def _insert_evidence(
    session,
    tenant_id: uuid.UUID,
    extraction_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    job: ExtractedJob,
    source: str,
) -> None:
    for name, field in vars(job).items():
        if not isinstance(field, ExtractedField):
            continue
        await session.execute(
            text(
                """
                INSERT INTO extraction_evidence
                    (id, tenant_id, extraction_id, opportunity_id, field_name,
                     extracted_value, evidence_text, start_char, end_char,
                     model_confidence, evidence_valid)
                VALUES (:id, :t, :ex, :op, :field, :value, :evidence, :start, :end,
                        :confidence, :valid)
                """
            ),
            {
                "id": uuid.uuid4(),
                "t": tenant_id,
                "ex": extraction_id,
                "op": opportunity_id,
                "field": name,
                "value": field.value,
                "evidence": field.evidence,
                "start": field.start_char,
                "end": field.end_char,
                "confidence": field.confidence,
                "valid": verify(field, source),
            },
        )
```

- [ ] **Step 5: Write the job**

Append to `app/workers/jobs.py`:

```python
async def extract_email(ctx, email_message_id: str) -> None:
    """Structured extraction (plan §12–§16)."""
    from app.services.ingest.extract import extract
    from app.services.ingest.persist import persist
    from app.services.ingest.preprocess import to_text
    from app.services.llm.client import LLMInvalidJSON

    located = await _locate(email_message_id)
    if located is None:
        return
    tenant_id, _, _, status = located
    if status not in ("classifying", "extracting"):
        return

    await _set_status(tenant_id, email_message_id, "extracting")

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT body_html_r2_key, subject, sender_email FROM email_messages"
                    " WHERE id = :i"
                ),
                {"i": email_message_id},
            )
        ).one()

    html = await body_store().get(row.body_html_r2_key) or ""
    source = to_text(html, subject=row.subject, sender=row.sender_email)

    try:
        response, result = await extract(source)
    except LLMInvalidJSON as exc:
        await _fail(tenant_id, email_message_id, str(exc))
        return

    ids = await persist(tenant_id, uuid.UUID(email_message_id), response, result, source)
    # A recruitment email with no vacancy in it is a successful outcome, not a
    # failure — it just did not contain what we were looking for.
    final = "extracted" if ids else "no_opportunity"
    await _set_status(tenant_id, email_message_id, final)


async def _fail(tenant_id: uuid.UUID, email_message_id: str, error: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE email_messages SET processing_status = 'failed',"
                " last_error = :e WHERE id = :i"
            ),
            {"e": error[:2000], "i": email_message_id},
        )
```

Register `extract_email` in `WorkerSettings.functions` in
`app/workers/settings.py`. The final registry is:

```python
functions = [
    fetch_email,
    classify_email,
    extract_email,
    backfill_mailbox_job,
    delta_sync_mailbox,
    recreate_subscription,
    reauthorize_subscription,
]
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_extract_job.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 7: Commit**

```bash
git add app/services/ingest tests/test_extract_job.py app/workers
git commit -m "Write verified vacancies from each recruitment email"
```

---

### Task 8: Retention and purge

**Files:**
- Create: `app/services/retention.py`
- Modify: `app/workers/tasks.py`, `app/workers/main.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `BodyStore`, `SessionLocal`
- Produces:
  - `async def purge_expired(store=None) -> int`
  - `def retention_for(classification_status: str, retention_months: int) -> timedelta`

- [ ] **Step 1: Write the failing test**

`tests/test_retention.py`:

```python
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.retention import purge_expired, retention_for
from app.services.storage.r2 import InMemoryBodyStore


def test_non_recruitment_mail_gets_the_short_horizon():
    """Long enough to catch a classifier mistake, short enough to be defensible."""
    assert retention_for("non_recruitment", 24) == timedelta(
        days=settings.NON_RECRUITMENT_RETENTION_DAYS
    )


def test_recruitment_mail_gets_the_tenant_horizon():
    assert retention_for("recruitment", 24) > timedelta(days=700)


def test_uncertain_mail_is_treated_as_recruitment():
    """Failing open at classification must not be undone by failing closed here."""
    assert retention_for("uncertain", 24) == retention_for("recruitment", 24)


@pytest.fixture
async def expired_row(admin_session):
    tid, mid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " body_r2_key, body_html_r2_key, retention_until, processing_status,"
            " source_state, classification_status) VALUES (:i, :t, :m, 'MSG', 'k.txt',"
            " 'k.html', now() - interval '1 day', 'extracted', 'present', 'recruitment')"
        ),
        {"i": eid, "t": tid, "m": mid},
    )
    await admin_session.commit()
    yield tid, eid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


async def test_purge_deletes_objects_and_nulls_the_keys(admin_session, expired_row):
    _, eid = expired_row
    store = InMemoryBodyStore()
    await store.put("k.txt", "body")
    await store.put("k.html", "<p>body</p>")

    await purge_expired(store=store)

    assert store.objects == {}
    row = (
        await admin_session.execute(
            text("SELECT body_r2_key, body_html_r2_key FROM email_messages WHERE id = :i"),
            {"i": eid},
        )
    ).one()
    assert row.body_r2_key is None
    assert row.body_html_r2_key is None


async def test_purge_never_deletes_the_row(admin_session, expired_row):
    """The row is the dedup entry. Delete it and the next delta walk re-ingests."""
    _, eid = expired_row

    await purge_expired(store=InMemoryBodyStore())

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM email_messages WHERE id = :i"), {"i": eid}
        )
    ).scalar_one()
    assert count == 1


async def test_purge_leaves_derived_opportunities_intact(admin_session, expired_row):
    tid, eid = expired_row
    await admin_session.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, email_message_id, job_title_raw,"
            " review_status, quality_state) VALUES (:i, :t, :e, 'Kept', 'ready', 'verified')"
        ),
        {"i": uuid.uuid4(), "t": tid, "e": eid},
    )
    await admin_session.commit()

    await purge_expired(store=InMemoryBodyStore())

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM opportunities WHERE email_message_id = :e"),
            {"e": eid},
        )
    ).scalar_one()
    assert count == 1


async def test_purge_is_idempotent(admin_session, expired_row):
    store = InMemoryBodyStore()

    first = await purge_expired(store=store)
    second = await purge_expired(store=store)

    assert first == 1
    assert second == 0, "already-purged rows must not be revisited"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retention.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.retention'`

- [ ] **Step 3: Write the service**

`app/services/retention.py`:

```python
"""Retention horizons and purging (spec: Retention; amends plan §2.3).

The principle is "preserve source provenance while the source is legitimately
retained", not "keep everything forever". Two consequences shape this module:

- Bodies go; rows stay. The row is the deduplication entry, and deleting it
  would make the next delta walk re-ingest, re-classify and re-pay for mail the
  system already decided about. Only tenant deletion removes rows.
- Derived opportunities outlive their source. They are the asset; the email was
  the evidence, and the evidence rows record what it said.
"""

import uuid
from datetime import timedelta

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services.storage.r2 import R2BodyStore

log = get_logger(__name__)

_DAYS_PER_MONTH = 30


def retention_for(classification_status: str, retention_months: int) -> timedelta:
    """How long this email's body is kept.

    `uncertain` is treated as recruitment: the classifier fails open by design,
    and applying the short horizon here would quietly undo that.
    """
    if classification_status == "non_recruitment":
        return timedelta(days=settings.NON_RECRUITMENT_RETENTION_DAYS)
    return timedelta(days=retention_months * _DAYS_PER_MONTH)


async def purge_expired(store=None) -> int:
    """Delete expired bodies. Returns the number of emails purged."""
    store = store or R2BodyStore()

    async with SessionLocal() as session:
        rows = (
            await session.execute(text("SELECT * FROM expired_email_bodies()"))
        ).all()

    purged = 0
    for row in rows:
        keys = [k for k in (row.body_r2_key, row.body_html_r2_key) if k]
        if keys:
            await store.delete(*keys)
        async with SessionLocal() as session:
            await session.execute(
                text("SELECT clear_email_body_keys(:i)"), {"i": row.id}
            )
            await session.commit()
        purged += 1

    if purged:
        log.info("retention_purged", emails=purged)
    return purged
```

- [ ] **Step 4: Add the two operator functions**

Create `alembic/versions/20260727_2000_retention_functions.py`. The purge sweeps
every tenant, so like the other supervisor sweeps it goes through narrow
`SECURITY DEFINER` functions rather than a role that bypasses RLS.

```python
"""retention functions

Revision ID: d63f9e1a4c77
Revises: <the extraction tables revision id>
"""

from alembic import op

from app.core.config import settings

revision = "d63f9e1a4c77"
down_revision = "<the extraction tables revision id>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    role = settings.DATABASE_APP_ROLE
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expired_email_bodies()
        RETURNS TABLE (id uuid, body_r2_key text, body_html_r2_key text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT e.id, e.body_r2_key, e.body_html_r2_key
            FROM email_messages e
            WHERE e.retention_until IS NOT NULL
              AND e.retention_until < now()
              AND (e.body_r2_key IS NOT NULL OR e.body_html_r2_key IS NOT NULL)
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION clear_email_body_keys(p_id uuid)
        RETURNS void
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            UPDATE email_messages
            SET body_r2_key = NULL, body_html_r2_key = NULL
            WHERE id = p_id
        $$
        """
    )
    op.execute(f'GRANT EXECUTE ON FUNCTION expired_email_bodies() TO "{role}"')
    op.execute(f'GRANT EXECUTE ON FUNCTION clear_email_body_keys(uuid) TO "{role}"')


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS expired_email_bodies()")
    op.execute("DROP FUNCTION IF EXISTS clear_email_body_keys(uuid)")
```

Apply:

```bash
uv run alembic upgrade head
```

- [ ] **Step 5: Stamp `retention_until` at fetch time**

In `app/workers/jobs.py`, add to the `fetch_email` UPDATE:

```python
                    retention_until = now() + make_interval(
                        days => (SELECT retention_months * 30 FROM mailboxes
                                 WHERE id = :mailbox)),
```

with `"mailbox": mailbox_id` in the parameter dict. Stamping at write time means
purging never has to recompute policy across history — a tenant changing its
retention setting affects new mail, not a retroactive deletion nobody asked for.

- [ ] **Step 6: Register the periodic task**

In `app/workers/tasks.py`:

```python
async def purge_retention() -> int:
    from app.services.retention import purge_expired

    return await purge_expired()
```

In `build_tasks()` in `app/workers/main.py`:

```python
        PeriodicTask("purge_expired", settings.PURGE_INTERVAL_SECONDS, _purge),
```

Add to `app/core/config.py` and `.env`:

```python
    PURGE_INTERVAL_SECONDS: float = 86400.0
```

- [ ] **Step 7: Run the whole suite**

```bash
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/retention.py app/workers alembic/versions app/core/config.py tests/test_retention.py
git commit -m "Purge expired bodies without losing the dedup trail"
```

---

### Task 9: End-to-end pipeline test and golden files

**Files:**
- Create: `tests/fixtures/emails/`, `tests/test_pipeline_end_to_end.py`
- Test: itself

**Interfaces:**
- Consumes: every module in both plans
- Produces: a test proving a notification becomes an opportunity with zero network access

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline_end_to_end.py`:

```python
"""The whole pipeline, no network.

This is the test that would have caught every integration bug the unit tests
individually miss: a notification arriving and an opportunity coming out the
other end.
"""

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from app.services.storage.r2 import InMemoryBodyStore

EMAIL_HTML = """
<p>Hi team,</p>
<table>
<tr><td>Position</td><td>Finance officer</td></tr>
<tr><td>Company</td><td>KLN Logistics</td></tr>
<tr><td>Salary</td><td>Up to $3500 per month</td></tr>
<tr><td>Location</td><td>Greenwich Drive</td></tr>
<tr><td>Duration</td><td>3 months</td></tr>
</table>
"""

GRAPH_MESSAGE = {
    "id": "MSG-E2E",
    "internetMessageId": "<e2e@example.com>",
    "subject": "Finance officer — KLN Logistics",
    "receivedDateTime": "2026-07-27T02:15:00Z",
    "hasAttachments": False,
    "from": {"emailAddress": {"name": "Evelyn Xie", "address": "evelynxie@example.com"}},
    "body": {"contentType": "html", "content": EMAIL_HTML},
    "bodyPreview": "Finance officer",
}


@pytest.fixture
async def wired(monkeypatch, admin_session):
    """Fake Graph, fake R2, fake LLM, real database, real jobs."""
    tid, mid = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions (id, tenant_id, mailbox_id, subscription_id,"
            " resource, client_state, expires_at, status) VALUES (:i, :t, :m, 'sub-e2e',"
            " 'r', 'state-e2e', now() + interval '1 day', 'active')"
        ),
        {"i": uuid.uuid4(), "t": tid, "m": mid},
    )
    await admin_session.commit()

    from app.api import graph_webhook
    from app.services.graph.client import GraphClient
    from app.workers import jobs

    store = InMemoryBodyStore()
    queued: list[tuple[str, dict]] = []

    async def fake_enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    async def fake_client(tenant_id, mailbox_id):
        return GraphClient(
            token="t",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=GRAPH_MESSAGE)),
        )

    monkeypatch.setattr(graph_webhook, "enqueue", fake_enqueue)
    monkeypatch.setattr(jobs, "enqueue", fake_enqueue)
    monkeypatch.setattr(jobs, "body_store", lambda: store)
    monkeypatch.setattr(jobs, "graph_client_for_mailbox", fake_client)

    yield tid, mid, queued, store

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


async def test_notification_becomes_an_opportunity(monkeypatch, admin_session, wired):
    tid, mid, queued, store = wired

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/graph/notifications",
            json={
                "value": [
                    {
                        "subscriptionId": "sub-e2e",
                        "clientState": "state-e2e",
                        "resourceData": {"id": "MSG-E2E"},
                    }
                ]
            },
        )
    assert response.status_code == 202

    from app.workers import jobs

    email_id = queued[0][1]["email_message_id"]
    await jobs.fetch_email({}, email_message_id=email_id)

    # Classification and extraction with canned model responses.
    from app.services.ingest import classify as classify_module
    from app.services.ingest import extract as extract_module
    from app.services.llm.client import FakeLLM, LLMResult

    monkeypatch.setattr(
        classify_module,
        "complete_json",
        FakeLLM({"is_job_order": True, "reason": "a vacancy"}),
    )
    await jobs.classify_email({}, email_message_id=email_id)

    source_key = store.objects
    assert source_key, "the body must be in R2 before extraction reads it"

    async def fake_extract(source, **kwargs):
        start = source.index("Finance officer")
        salary_at = source.index("Up to $3500 per month")
        from app.services.ingest.schema import ExtractionResponse

        return (
            ExtractionResponse.model_validate(
                {
                    "jobs": [
                        {
                            "job_title": {
                                "value": "Finance officer",
                                "evidence": "Finance officer",
                                "start_char": start,
                                "end_char": start + len("Finance officer"),
                                "confidence": 0.95,
                            },
                            "salary": {
                                "value": "3500",
                                "evidence": "Up to $3500 per month",
                                "start_char": salary_at,
                                "end_char": salary_at + len("Up to $3500 per month"),
                                "confidence": 0.9,
                            },
                            "salary_period": {
                                "value": "month",
                                "evidence": "Up to $3500 per month",
                                "start_char": salary_at,
                                "end_char": salary_at + len("Up to $3500 per month"),
                                "confidence": 0.9,
                            },
                        }
                    ]
                }
            ),
            LLMResult(data={}, model="test/fast"),
        )

    # `extract_email` imports `extract` inside the function body, so patching
    # the module attribute is what the job will actually resolve.
    monkeypatch.setattr(extract_module, "extract", fake_extract)
    await jobs.extract_email({}, email_message_id=email_id)

    row = (
        await admin_session.execute(
            text(
                "SELECT o.job_title_raw, o.salary_min, o.salary_period,"
                " o.received_datetime, o.quality_state, e.processing_status"
                " FROM opportunities o JOIN email_messages e ON e.id = o.email_message_id"
                " WHERE o.email_message_id = :i"
            ),
            {"i": email_id},
        )
    ).one()

    assert row.job_title_raw == "Finance officer"
    assert float(row.salary_min) == 3500.0
    assert row.salary_period == "month"
    assert row.received_datetime is not None
    assert row.quality_state in ("verified", "likely")
    assert row.processing_status == "extracted"


async def test_a_non_recruitment_email_costs_no_extraction(
    monkeypatch, admin_session, wired
):
    """Success criterion 6."""
    tid, mid, queued, store = wired

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/graph/notifications",
            json={
                "value": [
                    {
                        "subscriptionId": "sub-e2e",
                        "clientState": "state-e2e",
                        "resourceData": {"id": "MSG-E2E"},
                    }
                ]
            },
        )

    from app.services.ingest import classify as classify_module
    from app.services.llm.client import FakeLLM
    from app.workers import jobs

    email_id = queued[0][1]["email_message_id"]
    await jobs.fetch_email({}, email_message_id=email_id)

    monkeypatch.setattr(
        classify_module,
        "complete_json",
        FakeLLM({"is_job_order": False, "reason": "an invoice"}),
    )
    await jobs.classify_email({}, email_message_id=email_id)

    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, retention_until FROM email_messages"
                " WHERE id = :i"
            ),
            {"i": email_id},
        )
    ).one()
    assert row.processing_status == "skipped"
    assert row.retention_until is not None, "the short horizon must be stamped"

    assert not [name for name, _ in queued if name == "extract_email"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline_end_to_end.py -v`
Expected: FAIL, on whichever integration seam is not yet wired.

- [ ] **Step 3: Fix what it finds**

Work the failures until both pass. Do not weaken the assertions — this test is
the only place the modules meet, and every assertion in it corresponds to a
success criterion in the spec.

- [ ] **Step 4: Add golden files for extraction quality**

Save the five emails from the target screenshot as HTML under
`tests/fixtures/emails/`, with an expected-extraction JSON beside each. These
measure extraction *accuracy*, which is a different question from pipeline
correctness and is evaluated separately (plan §39 Stage 4) — they are not run
as ordinary unit tests.

- [ ] **Step 5: Run the whole suite**

```bash
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_pipeline_end_to_end.py tests/fixtures
git commit -m "Prove a notification becomes an opportunity offline"
```

---

## Self-Review

**Spec coverage.** Classifier and `classification_status` → Task 4; evidence
offsets and verification → Tasks 5 and 6; `quality_state` from deterministic
signals → Task 6; analytics-ready columns including `salary_period` and
`work_arrangement` → Task 1; multi-job per email → Tasks 1 and 7; extraction
provenance and replay → Tasks 1 and 7; human overrides never clobbered → Task 7;
retention horizons, body-only purge, dedup trail preserved → Task 8; the
`Not mentioned` policy → Tasks 5 and 7; model routing and escalation → Task 7;
end-to-end proof and success criteria 1, 4, 5, 6 → Task 9.

**Still deferred, and stated in the spec:** opportunity-level dedup (§19),
embeddings (§20), the review-queue UI (§21), attachment parsing, and the
taxonomy pass that populates `job_family` and `seniority`.

**Type consistency.** `email_message_id` is a `str` at every arq job boundary and
a `uuid.UUID` inside `persist()`; the conversion happens once, in
`extract_email`. `ExtractedField` is the only field type — `verify()`,
`quality_state()`, and `_insert_evidence()` all take it. `parse_salary()` returns
`(min, max, currency)` in that order in both `evidence.py` and `persist.py`.
`LLMResult` carries `.model`, `.prompt_tokens`, `.completion_tokens`,
`.latency_ms`, `.raw`, and `persist()` reads exactly those.
