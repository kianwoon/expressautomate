"""Render the stage outputs as the labelled text blocks the prompts read.

The model answers one stage's question from the previous stage's output, so
each upstream stage is passed into the next prompt as readable text rather
than as a JSON blob the model has to re-parse. Keeping the renderers here means
a prompt's view of its inputs is defined once, beside the schema it renders
from, and the prompt modules stay concerned only with what they ask.
"""

from app.services.job_intelligence.schema import CandidatePersona, JDUnderstanding


def understanding_text(understanding: JDUnderstanding) -> str:
    """A `JDUnderstanding` as a labelled block the persona/search prompts read."""
    lines = [f"Role: {understanding.role}"]
    if understanding.business_purpose:
        lines.append(f"Purpose: {understanding.business_purpose}")
    if understanding.work_environment:
        lines.append(f"Environment: {understanding.work_environment}")
    if understanding.working_conditions:
        lines.append(f"Conditions: {understanding.working_conditions}")
    if understanding.daily_activities:
        lines.append("Daily activities: " + "; ".join(understanding.daily_activities))
    if understanding.must_have_requirements:
        lines.append("Must have: " + "; ".join(understanding.must_have_requirements))
    return "\n".join(lines)


def persona_text(persona: CandidatePersona) -> str:
    """A `CandidatePersona` as a labelled block the search prompt reads."""
    lines: list[str] = []
    if persona.likely_backgrounds:
        lines.append("Backgrounds: " + "; ".join(persona.likely_backgrounds))
    if persona.transferable_roles:
        lines.append("Transferable roles: " + "; ".join(persona.transferable_roles))
    if persona.transferable_industries:
        lines.append(
            "Transferable industries: " + "; ".join(persona.transferable_industries)
        )
    if persona.career_stage:
        lines.append(f"Career stage: {persona.career_stage}")
    if persona.behaviours:
        lines.append("Behaviours: " + "; ".join(persona.behaviours))
    if persona.communication_style:
        lines.append(f"Communication: {persona.communication_style}")
    if persona.motivations:
        lines.append("Motivations: " + "; ".join(persona.motivations))
    if not lines:
        return "(no persona details)"
    return "\n".join(lines)
