"""Render the work decomposition as the labelled text the assessment prompt reads."""

from app.services.candidate_intelligence.schema import WorkAssessment


def work_text(work: WorkAssessment) -> str:
    """A `WorkAssessment` as a labelled block the assessment prompt reads.

    Each work unit is rendered in full — the assessment stage reasons about
    decision ownership, AI heavy-lift, and inflation per unit, so summarising
    here would strip the detail it needs.
    """
    lines: list[str] = []
    for role in work.roles:
        head = f"- {role.period} | {role.stated_title}"
        if role.employer:
            head += f" | {role.employer}"
        if role.industry:
            head += f" | {role.industry}"
        if role.tenure_months:
            head += f" | {role.tenure_months}mo"
        lines.append(head)
        if role.contribution_maturity:
            lines.append(f"    contribution: {role.contribution_maturity}")
        for wu in role.work_units:
            parts = [f"    • {wu.work}"]
            if wu.claim and wu.claim != wu.work:
                parts.append(f' [CV says: "{wu.claim}"]')
            if wu.inflated:
                parts.append(" ⚠ INFLATED")
            lines.append("".join(parts))
            if wu.decision_ownership:
                lines.append(f"      decision: {wu.decision_ownership}")
            if wu.complexity:
                lines.append(f"      complexity: {wu.complexity}")
            if wu.ai_heavy_lift:
                lines.append(f"      AI heavy-lift: {wu.ai_heavy_lift}")
            if wu.human_residual:
                lines.append(f"      human residual: {wu.human_residual}")
            if wu.evidence:
                tag = "⚠ " if wu.inflated else ""
                lines.append(f"      evidence: {tag}{wu.evidence}")
            if wu.evidence_note:
                lines.append(f"      note: {wu.evidence_note}")
    if work.education:
        edu = [
            f"  - {e.period}: {e.qualification}"
            + (f" — {e.institution}" if e.institution else "")
            for e in work.education
        ]
        lines.append("Education:\n" + "\n".join(edu))
    return "\n".join(lines)
