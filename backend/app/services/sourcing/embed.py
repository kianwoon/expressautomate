"""The text that becomes a vector, assembled under the same rules as the prompt.

Embeddings compare CVs to job orders by meaning, which is exactly what the
keyword-bound scorer cannot do — "React" and "ReactJS" share no token the set
intersection in `score.py` would catch, but they are a single concept in
embedding space. The catch is that whatever the embedding model reads is what
it ranks on, so the assembly here inherits the discipline the explanation
prompt already enforces: protected characteristics never enter the text, and
opportunity text is redacted before the model sees it.

**Candidate text is a whitelist.** A candidate row carries sex, race,
nationality and date of birth because a MOM form asks for them; none of those
may influence a similarity score, so none of them is concatenated here. This
is the same rule `MatchCandidate` applies in `explain.py`, kept in a separate
module because the embedding pipeline runs at parse time rather than at
sourcing time, and a column added to `Candidate` must not appear in the
embedded text by accident.

**Opportunity text is redacted.** The same `redact()` that strips
protected-attribute codes before the explanation prompt sees a job order
strips them before the embedding model does. A coded requirement that reaches
the embedding becomes a direction in vector space the cosine search would
happily obey — the laundering `redact.py` exists to prevent, at one remove.

Pure module — no database, no settings, no I/O. The cap is applied by the
caller via `settings.EMBEDDING_MAX_CHARS`, so a test asserts on untruncated
text and the worker owns the budget.
"""

from app.services.sourcing.redact import redact

# The separator between assembled fields. A newline gives each field its own
# line, which the embedding model reads as the structure of a CV/JD rather
# than as run-on prose. Whitespace-only fields are skipped, so the separator
# never appears twice in a row.
_SEP = "\n"


def _clean(value) -> str:
    """Strip and drop None, so absent fields contribute nothing."""
    return (value or "").strip()


def candidate_text_for_embedding(candidate, roles, skills) -> str:
    """The text that represents one candidate in embedding space.

    A bounded concatenation of the structured, job-related fields: current
    title, the titles and employers of past roles, and skills. Role
    descriptions are included when present because they carry the detail a
    title alone cannot — "Software Engineer" says nothing about whether the
    person built frontends or backends, and the description does.

    Deliberately excludes `candidates.sex`, `.race`, `.race_detail`,
    `.nationality`, `.date_of_birth`, `.education_years` and languages: those
    are eligibility facts for a job order to state, not material for a
    similarity score. See the whitelist rule on `MatchCandidate` in
    `explain.py` — the same boundary, applied here because the embedding
    pipeline runs without a model in the loop to push back.
    """
    parts: list[str] = []

    title = _clean(getattr(candidate, "current_title", None))
    if title:
        parts.append(title)

    employer = _clean(getattr(candidate, "current_employer", None))
    if employer:
        parts.append(employer)

    for role in roles or ():
        if getattr(role, "status", None) == "rejected":
            # A rejected role is one a human said did not happen. Embedding it
            # would credit experience the record denies — the same rule
            # `score.py` applies when computing tenure spans.
            continue
        role_title = _clean(getattr(role, "title", None))
        role_employer = _clean(getattr(role, "employer", None))
        if role_title:
            parts.append(role_title)
        if role_employer and role_employer != employer:
            # The current employer already appears above; repeating it per past
            # role would let the embedding weight one employer over the rest.
            parts.append(role_employer)
        description = _clean(getattr(role, "description", None))
        if description:
            parts.append(description)

    held = []
    for skill in skills or ():
        name = _clean(
            getattr(skill, "skill_normalized", None) or getattr(skill, "skill", None)
        )
        if name and name not in held:
            held.append(name)
    if held:
        parts.append("Skills: " + ", ".join(held))

    if not parts:
        return ""
    return _SEP.join(parts)


def opportunity_text_for_embedding(opportunity, codes=()) -> tuple[str, list[str]]:
    """The text that represents one job order in embedding space.

    The job description and requirements are the richest signal — they carry
    the detail a title abbreviates — so they lead. Every field goes through
    `redact()` first, because a coded protected-characteristic requirement
    embedded verbatim becomes a direction the cosine search would obey, which
    is the laundering this whole stack exists to prevent.

    Returns the assembled text and the codes that were removed, mirroring
    `redact()`'s own contract. The caller may log the removed codes for audit,
    the same way `explain_matches` stores them on the sourcing run.
    """
    fields = [
        _clean(getattr(opportunity, "job_title_raw", None)),
        _clean(getattr(opportunity, "job_description", None)),
        _clean(getattr(opportunity, "requirements", None)),
    ]
    # Skills are a list on the opportunity; join them so the embedding reads
    # them as part of the document rather than as a separate structure.
    wanted = getattr(opportunity, "skills", None)
    if wanted:
        skills_text = ", ".join(_clean(s) for s in wanted if _clean(s))
        if skills_text:
            fields.append("Skills: " + skills_text)

    cleaned: list[str] = []
    removed: list[str] = []
    for value in fields:
        if not value:
            continue
        text, hits = redact(value, list(codes or []))
        for code in hits:
            if code not in removed:
                removed.append(code)
        if text:
            cleaned.append(text)

    if not cleaned:
        return "", removed
    return _SEP.join(cleaned), removed


def truncate(text: str, max_chars: int) -> str:
    """Cap text at `max_chars` on a character boundary.

    The embedding model's own context limit is higher than this cap, which
    exists for cost and fairness: one very long CV should not dominate a batch
    or the per-candidate spend. Truncating on a raw character count is blunt
    but deterministic — a word or sentence boundary would be gentler and also
    make the cap depend on where the boundary falls, which is the wrong
    trade-off for a budget that has to be predictable.
    """
    if not text or max_chars <= 0:
        return ""
    return text[:max_chars]
