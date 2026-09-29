"""Compact text forms of the accessibility tree, history and diffs for prompts.

Each element gets a short id (`e1`, `e2`, ... depth-first) so the model
names an element instead of inventing a query; the provider maps the id
back to an `ElementQuery`. Lines are indented two spaces per depth:

    e3 button "Save" id=save_btn frame=20,700,350,44
    e4 textfield "Email" value="a@b.c" frame=20,120,350,44 disabled
"""

from __future__ import annotations

from swarmqa.llm.protocol import ChangedFile, HistoryStep, StepDecision
from swarmqa.models import ElementQuery, UIElement

MAX_ELEMENTS = 400
_LABEL_LIMIT = 80


def tree_text(elements: list[UIElement], *, max_elements: int = MAX_ELEMENTS) -> tuple[str, dict[str, UIElement]]:
    """Return the tree as indented lines and the id -> element index."""
    lines: list[str] = []
    index: dict[str, UIElement] = {}
    omitted = 0

    def visit(nodes: list[UIElement], depth: int) -> None:
        nonlocal omitted
        for element in nodes:
            if len(index) >= max_elements:
                omitted += 1 + _count(element.children)
                continue
            key = f"e{len(index) + 1}"
            index[key] = element
            lines.append("  " * depth + _line(key, element))
            visit(element.children, depth + 1)

    visit(elements, 0)
    if omitted:
        lines.append(f"[{omitted} more elements omitted]")
    return ("\n".join(lines) if lines else "(empty tree)"), index


def query_for(element: UIElement) -> ElementQuery:
    return ElementQuery(role=element.role, label=element.label or None, identifier=element.identifier)


def history_text(history: list[HistoryStep], *, limit: int) -> str:
    if not history:
        return "(no earlier steps)"
    recent = history[-limit:] if limit > 0 else []
    skipped = len(history) - len(recent)
    lines = [f"[{skipped} earlier steps omitted]"] if skipped else []
    start = skipped + 1
    for offset, step in enumerate(recent):
        parts = [f"{start + offset}. {describe_action(step.action)}"]
        if step.outcome:
            parts.append(f"-> {_clip(step.outcome, 160)}")
        if step.screen:
            parts.append(f"(screen: {_clip(step.screen, 60)})")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def describe_action(action: StepDecision) -> str:
    target = action.target
    if target is not None:
        bits = [target.role or "element"]
        if target.label:
            bits.append(f'"{_clip(target.label, 40)}"')
        if target.identifier:
            bits.append(f"id={target.identifier}")
        where = " ".join(bits)
    else:
        where = ""
    if action.kind == "tap":
        return f"tap {where}".strip()
    if action.kind == "type":
        return f'type "{_clip(action.text or "", 40)}" into {where or "focused field"}'
    if action.kind == "tap_point" and action.point:
        return f"tap at {action.point[0]:.0f},{action.point[1]:.0f}"
    if action.kind == "swipe" and action.point and action.end:
        return f"swipe {action.point[0]:.0f},{action.point[1]:.0f} -> {action.end[0]:.0f},{action.end[1]:.0f}"
    if action.kind == "key":
        return "key " + "+".join(action.keys)
    return action.kind


def files_text(diff: str, files: list[ChangedFile], *, max_chars: int) -> str:
    """Diff first, then each file. Past `max_chars` the rest is cut with a visible marker."""
    sections = ["## Diff", diff.strip() or "(empty diff)"]
    for changed in files:
        sections.append(f"## File: {changed.path}")
        sections.append(changed.content.rstrip() if changed.content else "(content not provided)")
    text = "\n\n".join(sections)
    if max_chars > 0 and len(text) > max_chars:
        cut = len(text) - max_chars
        text = text[:max_chars] + f"\n\n[truncated: {cut} more characters not shown]"
    return text


def _line(key: str, element: UIElement) -> str:
    parts = [key, element.role or "element"]
    if element.label:
        parts.append(f'"{_clip(element.label, _LABEL_LIMIT)}"')
    if element.identifier:
        parts.append(f"id={element.identifier}")
    if element.value:
        parts.append(f'value="{_clip(element.value, 40)}"')
    if element.frame is not None:
        parts.append("frame=" + ",".join(f"{v:.0f}" for v in element.frame))
    if not element.enabled:
        parts.append("disabled")
    return " ".join(parts)


def _count(nodes: list[UIElement]) -> int:
    return sum(1 + _count(node.children) for node in nodes)


def _clip(text: str, limit: int) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 3] + "..."
