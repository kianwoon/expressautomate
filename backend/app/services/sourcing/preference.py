"""Read a client's coded sex preference off the detected shorthand.

The glossary decodes client shorthand a recruiter forwarded — `C/F`, `O/F`,
`C/M`, `O/M` and the rest — into `OpportunityCode` rows, each carrying the
meaning the agency's glossary assigned at the time. Those rows are evidence of
what the client *asked for*, kept verbatim and (for the codes that name a
protected characteristic) redacted before any model reads the job text.

This module answers a different question than `redact.py` does, and the two
must not be confused:

- `redact.py` keeps a protected characteristic *out of the model's reasoning*.
  It exists so the platform never launders a discriminatory filter into a score.
- This module reads that same coded preference back, *for the recruiter's
  benefit*, so the shortlist does not hand a client who wrote `C/F` a row of
  male candidates. Acting on a client's stated preference is the recruiter's
  call (and a real legal exposure — see the sourcing orchestrator, which stamps
  every run it narrows); this function only turns the codes into a value, and
  decides nothing about who is filtered.

**Why `meaning`, not `attribute`.** The sex a code implies does not line up with
the `attribute` column: `C/F` is filed under `race` (it states race *and*
gender), while `O/F` is filed under `gender`. Filtering on `attribute ==
'gender'` would silently miss `C/F` — the most common code of all. The sex is
written into the human-readable `meaning` ("Chinese, female", "Any race, male")
in every starter code, so that is the field read here. A code whose meaning
names no sex ("Singapore Citizen", "Open — any race, any gender") contributes
nothing and is simply ignored.

**Agreement, not first-match.** An email that asks for `C/F` *and* `O/M` has
stated both sexes; narrowing to either would be guessing which role the client
meant, so a conflict yields `None` and the run is not narrowed at all. Only when
every sex-bearing code in the email agrees does a single sex come back.

Pure module — no database, no settings, no I/O.
"""

import re

FEMALE = "female"
MALE = "male"

# Word-boundary match, case-insensitive. `\bmale\b` does not fire inside
# "female" (no boundary between the two e's and m), and does not fire inside
# "Malay" either (different letters), so the two patterns are independent and
# order is the only guard that matters: `female` is tested first so its
# longer spelling is claimed before `male` can see the tail of it.
_FEMALE = re.compile(r"\bfemale\b", re.IGNORECASE)
_MALE = re.compile(r"\bmale\b", re.IGNORECASE)


def _sex_in(meaning: str) -> str | None:
    """The sex one code's meaning states, or `None` if it states none.

    "any gender" / "any race, any gender" name no sex and return `None`, which
    is what keeps `O/O` and the open-gender codes from narrowing a run.
    """
    if not meaning:
        return None
    if _FEMALE.search(meaning):
        return FEMALE
    if _MALE.search(meaning):
        return MALE
    return None


def implied_sex(codes) -> str | None:
    """The single sex a job order's coded shorthand implies, or `None`.

    Args:
        codes: `OpportunityCode` rows — anything exposing a `meaning` string,
            so this module stays free of the ORM model (the same stance
            `redact.py` takes with `code`/`attribute`).

    Returns:
        `FEMALE` when every sex-bearing code agrees on female, `MALE` when they
        agree on male, and `None` when they disagree, when none names a sex, or
        when there are no codes at all.
    """
    if not codes:
        return None

    implied: set[str] = set()
    for entry in codes:
        meaning = getattr(entry, "meaning", None)
        sex = _sex_in(meaning)
        if sex is not None:
            implied.add(sex)

    if len(implied) != 1:
        # Empty (no code names a sex) or conflicting (both named): neither is a
        # basis to narrow a shortlist on a protected characteristic.
        return None
    return next(iter(implied))
