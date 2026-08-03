"""Rendering is pure — no database, no network, no clock."""

import uuid

import pytest

from app.core.config import settings
from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    CHANNEL_WHATSAPP_LINKED,
)
from app.services.notify import render as render_module
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    MISSING,
    OpportunityEvent,
)
from app.services.notify.render import render


def _event(**overrides) -> OpportunityEvent:
    base = {
        "kind": EVENT_OPPORTUNITY_NEW,
        "tenant_id": uuid.uuid4(),
        "opportunity_id": uuid.uuid4(),
        "job_title": "Senior Backend Engineer",
        "company_name": "Acme Pte Ltd",
        "location": "Singapore",
        "salary": "SGD 8,000 - 10,000 monthly",
    }
    return OpportunityEvent(**{**base, **overrides})


def test_telegram_names_the_job_and_the_company() -> None:
    content = render(_event(), CHANNEL_TELEGRAM)
    assert "Senior Backend Engineer" in content.text
    assert "Acme Pte Ltd" in content.text


def test_telegram_says_not_mentioned_rather_than_inventing() -> None:
    """Plan section 15: an absent value is stated as absent."""
    content = render(_event(salary=None), CHANNEL_TELEGRAM)
    assert MISSING in content.text


def test_whatsapp_params_are_ordered_title_company_location_salary() -> None:
    """A swapped {{1}}/{{2}} reads as a job title at a company that does not
    exist, and Meta will deliver it happily."""
    content = render(_event(), CHANNEL_WHATSAPP)
    assert content.body_params == [
        "Senior Backend Engineer",
        "Acme Pte Ltd",
        "Singapore",
        "SGD 8,000 - 10,000 monthly",
    ]


def test_whatsapp_button_param_is_the_opportunity_id() -> None:
    event = _event()
    content = render(event, CHANNEL_WHATSAPP)
    assert content.button_param == str(event.opportunity_id)


def test_whatsapp_template_comes_from_config_not_source() -> None:
    new = render(_event(kind=EVENT_OPPORTUNITY_NEW), CHANNEL_WHATSAPP)
    review = render(
        _event(kind=EVENT_OPPORTUNITY_NEEDS_REVIEW), CHANNEL_WHATSAPP
    )
    assert new.template_name == settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW
    assert review.template_name == settings.WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW
    assert new.language == settings.WHATSAPP_TEMPLATE_LANG


def test_whatsapp_never_emits_an_empty_param() -> None:
    """Meta rejects a template whose parameter is an empty string, and the
    rejection arrives as a failed send with no obvious cause."""
    content = render(
        _event(job_title=None, company_name=None, location=None, salary=None),
        CHANNEL_WHATSAPP,
    )
    assert all(p for p in content.body_params)


def test_rollup_is_appended_to_telegram() -> None:
    content = render(_event(), CHANNEL_TELEGRAM, rollup=4)
    assert "4 more" in content.text


def test_rollup_does_not_change_whatsapp_param_count() -> None:
    """The template is approved with a fixed parameter count; adding one for a
    rollup would make every capped send fail."""
    plain = render(_event(), CHANNEL_WHATSAPP)
    rolled = render(_event(), CHANNEL_WHATSAPP, rollup=4)
    assert len(plain.body_params) == len(rolled.body_params)


def test_unknown_channel_is_an_error() -> None:
    with pytest.raises(ValueError):
        render(_event(), "carrier-pigeon")


def test_whatsapp_linked_gets_free_form_prose_not_a_template() -> None:
    """The recruiter's own device has no template regime and no 24-hour
    window — it gets the same prose Telegram does."""
    content = render(_event(), CHANNEL_WHATSAPP_LINKED)
    assert hasattr(content, "text")
    assert "Senior Backend Engineer" in content.text
    assert "Acme Pte Ltd" in content.text


def test_whatsapp_linked_never_consults_the_template_table(monkeypatch) -> None:
    """`_TEMPLATE_FOR` is the WABA's approved-template map; whatsapp_linked
    must never look a kind up in it, because that map only defines the four
    kinds `_whatsapp`/`_candidate_whatsapp` handle, and would KeyError or
    silently reuse a template for any kind that only the free-form channels
    render. A dict that raises on every access proves the free-form path
    never touches it."""

    class _ExplodingTemplateMap(dict):
        def __getitem__(self, key):
            raise AssertionError("whatsapp_linked must not consult _TEMPLATE_FOR")

    monkeypatch.setattr(render_module, "_TEMPLATE_FOR", _ExplodingTemplateMap())

    content = render(_event(), CHANNEL_WHATSAPP_LINKED)
    assert "Senior Backend Engineer" in content.text
