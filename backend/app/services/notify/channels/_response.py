"""Shared response-parsing helper for notification channel clients.

This exists because the same bug has now been fixed three times in three
places across telegram.py and whatsapp.py: every call site that did
`response.json()...` assumed the parsed body would be a dict, and that
assumption was wrong in the same way each time. `response.json()` raises
ValueError on unparseable bodies (round one), but a body that parses just
fine to `null`, `[]`, or `"a string"` sails through and then blows up with
AttributeError the moment something calls `.get()` on it (round two, on the
200 path; round three, on the non-200 path). Centralising the "give me a
dict or give me nothing" operation here means no call site needs its own
try/except or isinstance check, and a new call site added later inherits the
safety automatically instead of having a chance to reintroduce the gap.
"""

import httpx


def _json_object(response: httpx.Response) -> dict | None:
    """Parse the response body and return it only if it is a JSON object.

    Returns None — never raises — for an unparseable body, or for a body
    that parses to something other than an object (null, a list, a bare
    string/number). Returns the dict for anything that is genuinely an
    object, including a legitimately empty one (`{}`), so callers can tell
    "provider sent us nothing usable" (None) apart from "provider sent an
    empty-but-valid object" ({}) — collapsing those two would quietly change
    an outcome (e.g. SENT vs TRANSIENT) for a caller that used to
    distinguish them. Every other caller can still just do
    `(_json_object(response) or {}).get(...)` when the distinction doesn't
    matter to it.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None
