"""Shared element matching rules for live drivers and the fake driver."""

from __future__ import annotations

from swarmqa.errors import ElementNotFoundError
from swarmqa.models import ElementQuery, UIElement


def walk(elements: list[UIElement]):
    for element in elements:
        yield element
        yield from walk(element.children)


def find_element(elements: list[UIElement], query: ElementQuery) -> UIElement:
    if not any((query.role, query.label, query.identifier, query.value)):
        raise ElementNotFoundError("element query is empty")
    matches = [element for element in walk(elements) if matches_query(element, query)]
    if not matches:
        raise ElementNotFoundError(_describe(query))
    return matches[0]


def matches_query(element: UIElement, query: ElementQuery) -> bool:
    """Role compares case-insensitively. Label is a case-insensitive substring.

    Identifier and value match exactly when the query sets them.
    """
    if query.role and element.role.lower() != query.role.lower():
        return False
    if query.label and query.label.lower() not in (element.label or "").lower():
        return False
    if query.identifier and element.identifier != query.identifier:
        return False
    if query.value is not None and element.value != query.value:
        return False
    return True


def _describe(query: ElementQuery) -> str:
    parts = []
    if query.role:
        parts.append(f"role={query.role}")
    if query.label:
        parts.append(f"label={query.label!r}")
    if query.identifier:
        parts.append(f"id={query.identifier}")
    if query.value is not None:
        parts.append(f"value={query.value!r}")
    return "element not found: " + ", ".join(parts)
