"""Telling "somebody already has that" apart from a genuine fault.

Lived in `candidates.py` until `clients.py` needed the same judgement.
Importing it from a sibling endpoint module would make one endpoint depend on
another for no reason other than where the function happened to be written.
"""

from sqlalchemy.exc import IntegrityError

# Postgres SQLSTATE for a unique/exclusion violation. Only this class of
# integrity error means "somebody already has that"; every other one (a CHECK,
# a foreign key) is a different fault and must not be dressed up as a duplicate.
_UNIQUE_VIOLATION = "23505"


def is_duplicate(exc: IntegrityError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION
