"""One event, two very different shapes.

Telegram takes free-form text. WhatsApp does not: every message here is
business-initiated outside any 24-hour customer service window, which under
Meta's per-message pricing means a pre-approved *utility* template — ordered
positional parameters, no prose. The two renderers are genuinely different and
this module does not pretend otherwise by inventing a shared abstraction that
would fit neither.
"""

from dataclasses import dataclass

from app.core.config import settings
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    MISSING,
    OpportunityEvent,
)


@dataclass(frozen=True)
class TelegramContent:
    text: str


@dataclass(frozen=True)
class WhatsAppContent:
    template_name: str
    language: str
    body_params: list[str]
    button_param: str


_TEMPLATE_FOR = {
    EVENT_OPPORTUNITY_NEW: lambda: settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_NEEDS_REVIEW: lambda: settings.WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW,
}

# allow-hardcode: user-facing copy, not matching logic.
_HEADLINE = {
    EVENT_OPPORTUNITY_NEW: "New job order",
    EVENT_OPPORTUNITY_NEEDS_REVIEW: "Job order needs review",
}


def _or_missing(value: str | None) -> str:
    return value if value else MISSING


def render(
    event: OpportunityEvent, channel: str, rollup: int = 0
) -> TelegramContent | WhatsAppContent:
    """Content for one event on one channel.

    `rollup` is the count of sends suppressed by the rate cap since the last
    delivery. It is mentioned only on Telegram: the WhatsApp template is
    approved with a fixed parameter count, so adding one would make every
    capped send fail — which is the send that matters most.
    """
    if channel == CHANNEL_TELEGRAM:
        return _telegram(event, rollup)
    if channel == CHANNEL_WHATSAPP:
        return _whatsapp(event)
    raise ValueError(f"Unknown notification channel: {channel!r}")


def _telegram(event: OpportunityEvent, rollup: int) -> TelegramContent:
    lines = [
        f"*{_HEADLINE[event.kind]}*",
        f"{_or_missing(event.job_title)} — {_or_missing(event.company_name)}",
        f"Location: {_or_missing(event.location)}",
        f"Salary: {_or_missing(event.salary)}",
    ]
    if rollup:
        lines.append(f"_and {rollup} more while notifications were rate-limited_")
    return TelegramContent(text="\n".join(lines))


def _whatsapp(event: OpportunityEvent) -> WhatsAppContent:
    return WhatsAppContent(
        template_name=_TEMPLATE_FOR[event.kind](),
        language=settings.WHATSAPP_TEMPLATE_LANG,
        # Order is the contract with the approved template. Changing it here
        # without resubmitting the template produces a delivered message that
        # reads as a job title at a company that does not exist.
        body_params=[
            _or_missing(event.job_title),
            _or_missing(event.company_name),
            _or_missing(event.location),
            _or_missing(event.salary),
        ],
        button_param=str(event.opportunity_id),
    )
