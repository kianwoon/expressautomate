"""Translate a WhatsApp draft through Google Translate's free endpoint.

The recruiter-facing WhatsApp modal lets the recruiter render the outreach
message in another language before they send it. The draft itself is always
English (`whatsapp_draft_text` in `app/api/candidate_whatsapp.py`); this
service turns that English text into one of the supported target languages on
request, so the canonical wording lives in one place and a switch between two
non-English languages re-translates from English rather than from each other.

The endpoint used is the same `translate_a/single` query a browser makes to
`translate.google.com`. It is unauthenticated and returns no CORS headers, so
the call has to run server-side (httpx) — which also keeps the recruiter's
browser off Google's blocklist. There is no API key, no billing account, and
no new dependency beyond httpx, which the gateway client already uses. The
trade-off is the usual one for an unofficial endpoint: it can rate-limit or
change shape, and a failure here surfaces to the recruiter as "try again or
keep it in English" rather than as a silent English fallback — a candidate
would otherwise read a message the recruiter never chose to send.
"""

import httpx

# The languages the WhatsApp modal offers, keyed by the wire value that travels
# in the API body. `english` is handled by the caller and never reaches this
# service; it is excluded here so an unknown key cannot reach the network by
# accident.
TARGET_LANGUAGE_CODES: dict[str, str] = {
    "chinese": "zh-CN",
    "malay": "ms",
    "tamil": "ta",
}

# A desktop browser UA. The default httpx user agent is sometimes answered with
# a 403, which reads as "translation is broken" to a recruiter who just wants
# the message in Chinese — pretending to be an ordinary browser is the
# documented workaround for the free endpoint.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TranslateError(Exception):
    """Google Translate could not be reached or returned an unusable answer.

    Carries a recruiter-readable message; the route turns this into a 502
    rather than letting the cause through, because an httpx
    `ConnectError`/`ReadTimeout` is not something a recruiter can act on.
    """


async def translate_message(
    source_text: str,
    target_language: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Translate `source_text` (English) into `target_language`.

    `target_language` must be a key of `TARGET_LANGUAGE_CODES`; the caller
    validates this and never passes `"english"` here.

    `transport` is the seam tests use — nothing in production passes it, and
    it is what keeps the suite from ever hitting the real network.

    The response is the nested array Google returns:
    `[[["你好世界", "Hello world", null, null, 10], ...], null, "en", ...]`.
    Each element of `response[0]` is one segment's `[translation, original,
    ...]`, so joining index 0 of every segment rebuilds the full message,
    including the line breaks the draft put between paragraphs.
    """
    code = TARGET_LANGUAGE_CODES.get(target_language)
    if code is None:
        # The route validates input first, so reaching here is a programming
        # error — raise loudly rather than silently no-op.
        raise TranslateError(f"Unsupported language: {target_language!r}")

    params = {
        "client": "gtx",
        "sl": "en",
        "tl": code,
        "dt": "t",
        "q": source_text,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            transport=transport,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/plain, */*",
            },
        ) as client:
            response = await client.get(
                "https://translate.google.com/translate_a/single",
                params=params,
            )
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise TranslateError(
            "We couldn't reach the translation service just now."
        ) from exc

    segments = body[0] if isinstance(body, list) and body else None
    if not segments or not isinstance(segments, list):
        raise TranslateError("The translation service gave an unexpected answer.")
    parts: list[str] = []
    for segment in segments:
        # Each segment is `[translation, original, ...]`. A segment missing its
        # translation, or a top-level element that is not a list (Google emits
        # `null` in several later positions), is skipped rather than crashed on
        # — the translated text is index 0, and an empty segment contributes
        # nothing.
        if isinstance(segment, list) and segment and isinstance(segment[0], str):
            parts.append(segment[0])
    if not parts:
        raise TranslateError("The translation service gave an unexpected answer.")
    return "".join(parts)
