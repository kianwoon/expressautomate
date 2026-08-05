"""The SQL vector column type for embeddings, decoupled from the pgvector import.

`pgvector.sqlalchemy.Vector` is the real type against a Postgres+pgvector
database. Importing the application's models should not require the package to
be installed, though — the test suite, the migration generator, and any cold
import path run in environments where it may be absent. This module is the
single place that asks for it, and falls back to a plain `Text` type so that
`from app.models.candidate import ...` never raises merely because pgvector is
missing.

The fallback is `Text` rather than a real vector type because the only code
that touches the column at runtime — the embeddings worker and the ANN query —
guards on `settings.embedding_configured()` and is exercised only in an
environment that has the package and the extension. The fallback exists so the
import graph does not break; the type it provides is never used in anger.
"""

try:  # pragma: no cover - exercised by whichever branch the env selects
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover
    from sqlalchemy import Text

    def Vector(dim: int | None = None):  # type: ignore[misc]
        """Stand-in used only when pgvector is not installed.

        Matches the call shape (`Vector(1536)`) so model definitions are
        identical with or without the package, and returns `Text` so the column
        type resolves to something SQLAlchemy can compile.
        """
        return Text()
