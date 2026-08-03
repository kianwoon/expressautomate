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
from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    CHANNEL_WHATSAPP_LINKED,
)
from app.services.notify.candidate_events import CandidateEvent
from app.services.notify.events import (
    CANDIDATE_ACCESS_DECLINED,
    CANDIDATE_ACCESS_GRANTED,
    CANDIDATE_ACCESS_REQUESTED,
    CANDIDATE_ASSIGNED,
    CANDIDATE_EVENT_KINDS,
    CANDIDATE_SHARED,
    CANDIDATE_UNCLAIMED,
    EVENT_OPPORTUNITY_ASSIGNED,
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_SHARED,
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
    # Share and assignment carry exactly the four job-order parameters the
    # `OPPORTUNITY_NEW` template is already approved for, so they reuse it
    # rather than adding a second approval — and, more to the point, rather
    # than adding a setting that would ship EMPTY and fail every send.
    EVENT_OPPORTUNITY_SHARED: lambda: settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_ASSIGNED: lambda: settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW,
    # All six candidate kinds share one approved template — same four
    # parameters, and six near-identical templates would be six approvals to
    # keep in step. Built from the kind tuple rather than listed twice, so a
    # kind added to `events.py` cannot be left out of this dict and become a
    # KeyError in the delivery worker.
    **{
        kind: (lambda: settings.WHATSAPP_TEMPLATE_CANDIDATE_UPDATE)
        for kind in CANDIDATE_EVENT_KINDS
    },
}

# allow-hardcode: user-facing copy, not matching logic.
_HEADLINE = {
    EVENT_OPPORTUNITY_NEW: "New job order",
    EVENT_OPPORTUNITY_NEEDS_REVIEW: "Job order needs review",
    EVENT_OPPORTUNITY_SHARED: "Job order shared with you",
    EVENT_OPPORTUNITY_ASSIGNED: "Job order assigned to you",
    CANDIDATE_SHARED: "Candidate shared with you",
    CANDIDATE_ASSIGNED: "Candidate handed over to you",
    CANDIDATE_UNCLAIMED: "Candidate released to the queue",
    CANDIDATE_ACCESS_REQUESTED: "Candidate access requested",
    CANDIDATE_ACCESS_GRANTED: "Candidate access granted",
    CANDIDATE_ACCESS_DECLINED: "Candidate access declined",
}


# The channels that take prose. `whatsapp_linked` goes out on the recruiter's
# own device, where Meta's template regime does not apply at all, so it gets the
# same text Telegram does — the rollup line included. Nothing about the shared
# WABA's `_TEMPLATE_FOR` changes.
_FREE_FORM_CHANNELS = frozenset({CHANNEL_TELEGRAM, CHANNEL_WHATSAPP_LINKED})


def _or_missing(value: str | None) -> str:
    return value if value else MISSING


def render(
    event: OpportunityEvent | CandidateEvent, channel: str, rollup: int = 0
) -> TelegramContent | WhatsAppContent:
    """Content for one event on one channel.

    `rollup` is the count of sends suppressed by the rate cap since the last
    delivery. It is mentioned only on the free-form channels: the WhatsApp
    template is approved with a fixed parameter count, so adding one would make
    every capped send fail — which is the send that matters most.
    """
    if isinstance(event, CandidateEvent):
        if channel in _FREE_FORM_CHANNELS:
            return _candidate_telegram(event, rollup)
        if channel == CHANNEL_WHATSAPP:
            return _candidate_whatsapp(event)
        raise ValueError(f"Unknown notification channel: {channel!r}")
    if channel in _FREE_FORM_CHANNELS:
        return _telegram(event, rollup)
    if channel == CHANNEL_WHATSAPP:
        return _whatsapp(event)
    raise ValueError(f"Unknown notification channel: {channel!r}")


def _candidate_telegram(event: CandidateEvent, rollup: int) -> TelegramContent:
    """Who did what to which person. No salary, no company — a candidate has
    neither, and inventing the fields to reuse the job-order shape is what
    the separate event type exists to avoid."""
    lines = [
        f"*{_HEADLINE[event.kind]}*",
        _or_missing(event.candidate_name),
    ]
    if event.actor_name:
        lines.append(f"By: {event.actor_name}")
    if event.note:
        lines.append(f"Note: {event.note}")
    if rollup:
        lines.append(f"_and {rollup} more while notifications were rate-limited_")
    return TelegramContent(text="\n".join(lines))


def _candidate_whatsapp(event: CandidateEvent) -> WhatsAppContent:
    return WhatsAppContent(
        template_name=_TEMPLATE_FOR[event.kind](),
        language=settings.WHATSAPP_TEMPLATE_LANG,
        # Fixed count and order, as with the job-order template above. Every
        # parameter goes through `_or_missing`: a blank parameter is rejected
        # by Meta outright, and `actor_name`/`note` are legitimately absent on
        # a row the delivery worker rebuilt from the database.
        body_params=[
            _HEADLINE[event.kind],
            _or_missing(event.candidate_name),
            _or_missing(event.actor_name),
            _or_missing(event.note),
        ],
        button_param=str(event.candidate_id),
    )


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
