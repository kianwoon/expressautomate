"""Render the stage outputs as the labelled text blocks the prompts read.

The model answers one stage's question from the previous stage's output, so
each upstream stage is passed into the next prompt as readable text rather
than as a JSON blob the model has to re-parse. Keeping the renderers here means
a prompt's view of its inputs is defined once, beside the schema it renders
from, and the prompt modules stay concerned only with what they ask.

Mirrors `job_intelligence/render.py` in shape.
"""

from app.services.candidate_intelligence.schema import (
    AutomationProfile,
    GapAnalysis,
    HistoryProfile,
    MarketBenchmark,
)


def history_text(history: HistoryProfile) -> str:
    """A `HistoryProfile` as a labelled block the downstream prompts read.

    The decomposed `work` per role is the input the automation stage reasons
    about, so it is rendered in full rather than summarised — summarising here
    would strip the very detail Layer 3 needs.
    """
    lines: list[str] = []
    if history.roles:
        role_lines: list[str] = []
        for role in history.roles:
            head = f"  - {role.period or '?'}: {role.title or '(untitled)'}"
            if role.domain:
                head += f" ({role.domain})"
            if role.seniority:
                head += f" [{role.seniority}]"
            role_lines.append(head)
            if role.scope:
                role_lines.append(f"      scope: {role.scope}")
            if role.evidence:
                role_lines.append(f"      evidence: {role.evidence}")
            for w in role.work:
                parts = [f"task: {w.task}"] if w.task else []
                if w.tool:
                    parts.append(f"tool: {w.tool}")
                if w.judgment_level:
                    parts.append(f"judgment: {w.judgment_level}")
                if w.accountability:
                    parts.append(f"accountability: {w.accountability}")
                if parts:
                    role_lines.append("      - " + ", ".join(parts))
        lines.append("Roles & decomposed work:\n" + "\n".join(role_lines))
    if history.industries:
        lines.append("Industries: " + ", ".join(history.industries))
    if history.functions:
        lines.append("Functions: " + ", ".join(history.functions))
    if history.systems:
        lines.append("Systems: " + ", ".join(history.systems))
    if history.trajectory:
        lines.append("Trajectory: " + " → ".join(history.trajectory))
    return "\n".join(lines)


def automation_text(automation: AutomationProfile) -> str:
    """An `AutomationProfile` as a labelled block the gaps/residual prompts read."""
    lines: list[str] = []
    if automation.assessments:
        entries = [
            f"  - {a.capability}: automation {a.automation_level}"
            + (f" — {a.automation_reason}" if a.automation_reason else "")
            + (
                f" | residual human value: {a.residual_human_value}"
                if a.residual_human_value
                else ""
            )
            for a in automation.assessments
        ]
        lines.append("Automation assessments:\n" + "\n".join(entries))
    if automation.scarce_capabilities:
        lines.append("Scarce capabilities: " + ", ".join(automation.scarce_capabilities))
    return "\n".join(lines)


def benchmark_text(benchmark: MarketBenchmark) -> str:
    """A `MarketBenchmark` as a labelled block the gaps/residual prompts read."""
    lines: list[str] = []
    if benchmark.work_family:
        lines.append(f"Work family: {benchmark.work_family}")
    if benchmark.current_work:
        lines.append("Current work: " + "; ".join(benchmark.current_work))
    if benchmark.current_required:
        lines.append("Current required capabilities: " + ", ".join(benchmark.current_required))
    if benchmark.declining:
        lines.append("Declining: " + ", ".join(benchmark.declining))
    if benchmark.emerging:
        lines.append("Emerging: " + ", ".join(benchmark.emerging))
    if benchmark.scarce:
        lines.append("Scarce: " + ", ".join(benchmark.scarce))
    if benchmark.automation_summary:
        lines.append(f"Automation summary: {benchmark.automation_summary}")
    return "\n".join(lines)


def gaps_text(gaps: GapAnalysis) -> str:
    """A `GapAnalysis` as a labelled block the residual prompt reads."""
    lines: list[str] = []
    if gaps.gaps:
        entries = [
            f"  - {g.capability}: {g.status}" + (f" — {g.note}" if g.note else "")
            for g in gaps.gaps
        ]
        lines.append("Capability gaps:\n" + "\n".join(entries))
    if gaps.evidence_gaps:
        lines.append("Evidence gaps: " + "; ".join(gaps.evidence_gaps))
    return "\n".join(lines)
