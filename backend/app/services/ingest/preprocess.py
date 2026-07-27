"""HTML to model-ready text (plan §11).

This module is the single source of truth for what "the text" of an email is.
Extraction asks the model for character offsets and then verifies them by
slicing the string this function returns, so any change to the transformation
silently invalidates every stored offset. Two properties follow from that:

1. **Deterministic.** The same HTML must always produce identical text.
   Anything order-dependent, locale-dependent, or randomised here turns
   verified evidence into off-by-N garbage on the next run.
2. **Conservative.** Forwarded chains and quoted replies are where the job
   order usually lives — a recruiter forwards the client's mail rather than
   retyping it. Trimming signatures or quote blocks aggressively is how you
   drop the vacancy and never find out, so nothing is trimmed by content.

The subject and sender are prepended as part of the text rather than passed
separately, because the position and salary are often only in the subject line
and evidence must be able to point at them.
"""

from selectolax.parser import HTMLParser

# Removed outright: their text is markup or code, never email content, and
# leaking `alert(1)` or a CSS rule into the prompt is both noise and an
# injection surface.
_DROP_TAGS = ("script", "style", "head", "meta", "link")

# Tags whose boundaries carry meaning. Without a break, adjacent table cells
# flatten to "SalaryUp to $3500" and a bullet list to one run-on line, which
# costs the model the field/value pairing that job orders rely on.
# `td`/`th` are here on top of the plan's list for the reason the plan gives:
# without a break between cells a two-column job table flattens to
# "SalaryUp to $3500", which no model reads correctly.
_BLOCK_TAGS = (
    "p",
    "div",
    "br",
    "table",
    "tr",
    "td",
    "th",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
)


def _walk(node) -> list[str]:
    """Depth-first text with an explicit newline at every block boundary.

    Written out rather than using `text(separator=...)`: the separator applies
    between all text nodes, so inline markup like `<b>$3,700</b>-<b>$4,500</b>`
    would be broken across lines and the salary range would no longer match any
    single evidence span. Emitting breaks only for block tags keeps inline runs
    intact while still splitting cells and list items.
    """
    if node.tag == "-text":
        return [node.text_content or ""]

    parts: list[str] = []
    if node.tag in _BLOCK_TAGS:
        parts.append("\n")
    for child in node.iter(include_text=True):
        parts.extend(_walk(child))
    if node.tag in _BLOCK_TAGS:
        parts.append("\n")
    return parts


def to_text(html: str, *, subject: str | None = None, sender: str | None = None) -> str:
    """Flatten HTML to text, preserving line structure and table cells."""
    tree = HTMLParser(html or "")
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    body = tree.body or tree.root
    raw = "".join(_walk(body)) if body is not None else ""

    # Stripping and dropping blank lines keeps the output stable across the
    # whitespace noise that mail clients emit differently for identical content.
    lines = [line.strip() for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)

    header = []
    if subject:
        header.append(f"SUBJECT: {subject}")
    if sender:
        header.append(f"SENDER: {sender}")
    return "\n".join([*header, text]) if header else text
