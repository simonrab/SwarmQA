"""Thin accessibility-tree summaries for decision backends."""

from __future__ import annotations

from swarmqa.models import UIElement


def summarize_tree(elements: list[UIElement], *, max_depth: int = 4) -> list[dict]:
    """Return a shallow dict summary of the tree for model backends."""

    def walk(nodes: list[UIElement], depth: int) -> list[dict]:
        if depth > max_depth:
            return []
        rows: list[dict] = []
        for element in nodes:
            row: dict = {
                "role": element.role,
                "label": element.label,
                "enabled": element.enabled,
            }
            if element.identifier:
                row["identifier"] = element.identifier
            if element.children and depth < max_depth:
                row["children"] = walk(element.children, depth + 1)
            rows.append(row)
        return rows

    return walk(elements, 1)
