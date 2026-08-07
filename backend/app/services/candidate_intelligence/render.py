"""Render the stage outputs as the labelled text blocks the prompts read.

The model answers one stage's question from the previous stage's output, so
each upstream stage is passed into the next prompt as readable text rather than
as a JSON blob the model has to re-parse. Keeping the renderers here means a
prompt's view of its inputs is defined once, beside the schema it renders from,
and the prompt modules stay concerned only with what they ask.

Mirrors `job_intelligence/render.py` in shape.
"""

from app.services.candidate_intelligence.schema import (
    CapabilityProfile,
    CareerProfile,
)


def career_text(career: CareerProfile) -> str:
    """A `CareerProfile` as a labelled block the capability/profile prompts read."""
    lines: list[str] = []
    if career.timeline:
        rungs = [
            f"  - {entry.period}: {entry.title} ({entry.domain})"
            for entry in career.timeline
        ]
        lines.append("Timeline:\n" + "\n".join(rungs))
    if career.trajectory:
        lines.append("Trajectory: " + " → ".join(career.trajectory))
    if career.primary_domain:
        lines.append(f"Primary domain: {career.primary_domain}")
    if career.secondary_domains:
        lines.append("Secondary domains: " + ", ".join(career.secondary_domains))
    if career.career_direction:
        lines.append(f"Direction: {career.career_direction}")
    if career.career_stage:
        lines.append(f"Stage: {career.career_stage}")
    return "\n".join(lines)


def capability_text(capability: CapabilityProfile) -> str:
    """A `CapabilityProfile` as a labelled block the profile prompt reads."""
    lines: list[str] = []
    if capability.capabilities:
        entries = [
            f"  - {entry.capability} ({entry.category}, {entry.confidence:.2f})"
            for entry in capability.capabilities
        ]
        lines.append("Capabilities:\n" + "\n".join(entries))
    if capability.tools:
        lines.append("Tools: " + ", ".join(capability.tools))
    return "\n".join(lines)
