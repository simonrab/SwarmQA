"""Tree helpers shared by the checks: role groups, frames, and structure."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterator

from swarmqa.models import ElementQuery, UIElement

Frame = tuple[float, float, float, float]

TEXT_ROLES = frozenset({"text", "statictext", "label"})
MODAL_ROLES = frozenset({"alert", "sheet", "dialog", "popover", "actionsheet"})
LAYER_ROLES = frozenset({"window", "menu", "menubar"}) | MODAL_ROLES
SCROLL_ROLES = frozenset(
    {"scrollarea", "scrollview", "table", "list", "collectionview", "outline", "webview", "browser"}
)
TOGGLE_ROLES = frozenset({"checkbox", "switch", "toggle", "radio", "radiobutton"})
FIELD_ROLES = frozenset(
    {"textfield", "securetextfield", "searchfield", "textarea", "textview", "combobox"}
)
# Controls a person taps or clicks. Menus are left out: their items are only
# on screen while the menu is open and their sizes follow the system.
INTERACTIVE_ROLES = (
    frozenset(
        {
            "button",
            "imagebutton",
            "link",
            "cell",
            "tab",
            "popupbutton",
            "slider",
            "stepper",
            "segmentedcontrol",
            "incrementor",
        }
    )
    | TOGGLE_ROLES
    | FIELD_ROLES
)


def norm_role(role: str | None) -> str:
    """Lower-case role with any `AX` prefix removed (`AXButton` -> `button`)."""
    text = (role or "").strip().lower()
    if text.startswith("ax") and len(text) > 2:
        text = text[2:]
    return text


@dataclass
class Node:
    """A tree element with its ancestry, in depth-first order."""

    element: UIElement
    index: int
    ancestors: tuple[int, ...]
    layer: int

    @property
    def role(self) -> str:
        return norm_role(self.element.role)


def nodes(tree: list[UIElement]) -> list[Node]:
    """Flatten `tree` depth-first. `layer` is the index of the nearest window/modal ancestor (-1 if none)."""
    out: list[Node] = []

    def visit(elements: list[UIElement], ancestors: tuple[int, ...], layer: int) -> None:
        for element in elements:
            index = len(out)
            node = Node(element=element, index=index, ancestors=ancestors, layer=layer)
            out.append(node)
            child_layer = index if node.role in LAYER_ROLES else layer
            visit(element.children, ancestors + (index,), child_layer)

    visit(tree, (), -1)
    return out


BAR_ROLES = frozenset({"navigationbar", "toolbar", "tabbar"})
PROGRESS_ROLES = frozenset({"activityindicator", "progressindicator", "busyindicator"})


def screen_frame(tree: list[UIElement], size: tuple[float, float] | None) -> Frame | None:
    """The screen in points: `size` when known, else the first root frame."""
    if size is not None and size[0] > 0 and size[1] > 0:
        return (0.0, 0.0, float(size[0]), float(size[1]))
    for element in tree:
        frame = usable_frame(element)
        if frame is not None:
            return frame
    return None


def stale_nodes(flat: list[Node], screen: Frame | None, *, tolerance: float = 1.0) -> set[int]:
    """Indices of nodes on a page that is sliding in or out, not the page on screen.

    During (and briefly after) a navigation push or pop on iOS, the snapshot
    holds both pages. The page that is leaving or arriving sits in a
    container exactly the size of the screen but shifted off the origin (the
    previous page is parallaxed to about x = -30% of the width). A node
    belongs to the page of its nearest screen-sized ancestor (or itself); it
    is stale when that page is off the origin. The page on screen starts
    again at the origin, so it is not stale even when the snapshot nests it
    inside the old page's container.
    """
    if screen is None:
        return set()
    _, _, width, height = screen
    state: dict[int, bool] = {}
    stale: set[int] = set()
    for node in flat:
        frame = node.element.frame
        own: bool | None = None
        if frame is not None:
            x, y, w, h = frame
            if abs(w - width) <= tolerance and abs(h - height) <= tolerance:
                own = abs(x - screen[0]) > tolerance or abs(y - screen[1]) > tolerance
        if own is None:
            own = state.get(node.ancestors[-1], False) if node.ancestors else False
        state[node.index] = own
        if own:
            stale.add(node.index)
    return stale


def has_ancestor_role(node: Node, flat: list[Node], roles: frozenset[str]) -> bool:
    return any(flat[index].role in roles for index in node.ancestors)


def is_scroll_bar(element: UIElement) -> bool:
    """UIKit's scroll indicators ("Vertical scroll bar, 1 page"); they flash on any touch."""
    return "scroll bar" in (element.label or "").lower()


def walk(tree: list[UIElement]) -> Iterator[UIElement]:
    for element in tree:
        yield element
        yield from walk(element.children)


def usable_frame(element: UIElement) -> Frame | None:
    """The element frame when it has a positive area, else None (hidden or unlaid-out)."""
    frame = element.frame
    if frame is None:
        return None
    x, y, width, height = frame
    if width <= 0 or height <= 0:
        return None
    return (float(x), float(y), float(width), float(height))


def area(frame: Frame) -> float:
    return max(0.0, frame[2]) * max(0.0, frame[3])


def intersection(a: Frame, b: Frame) -> Frame | None:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


def contains(outer: Frame, inner: Frame, *, tolerance: float = 1.0) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[0] + inner[2] <= outer[0] + outer[2] + tolerance
        and inner[1] + inner[3] <= outer[1] + outer[3] + tolerance
    )


def query_for(element: UIElement) -> ElementQuery:
    return ElementQuery(
        role=element.role or None,
        label=element.label or None,
        identifier=element.identifier or None,
    )


def describe(element: UIElement) -> str:
    name = element.label or element.identifier or ""
    role = norm_role(element.role) or "element"
    return f'{role} "{name}"' if name else role


def structure_signature(tree: list[UIElement]) -> str:
    """Hash of every element's role, label, identifier, value, enabled flag and frame.

    Frames are rounded to whole points so sub-point layout jitter does not
    count as a change.
    """
    parts: list[str] = []

    def visit(elements: list[UIElement], depth: int) -> None:
        for element in elements:
            frame = element.frame
            frame_text = "" if frame is None else ",".join(str(round(v)) for v in frame)
            parts.append(
                "\x1f".join(
                    (
                        str(depth),
                        norm_role(element.role),
                        element.label or "",
                        element.identifier or "",
                        element.value or "",
                        "1" if element.enabled else "0",
                        frame_text,
                    )
                )
            )
            visit(element.children, depth + 1)

    visit(tree, 0)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def visible_signature(tree: list[UIElement], size: tuple[float, float] | None = None) -> str:
    """Hash of what is on the page on screen, for telling whether an action changed anything.

    Like `structure_signature` but order- and nesting-free, and it leaves out
    nodes on a stale (sliding) page, scroll indicators and their children
    (UIKit flashes them on any touch in a scroll view), and anonymous
    containers (role `other` with no label, identifier or value), whose
    nesting changes while a navigation transition settles.
    """
    flat = nodes(tree)
    stale = stale_nodes(flat, screen_frame(tree, size))
    scroll_bars: set[int] = set()
    items: list[str] = []
    for node in flat:
        element = node.element
        if is_scroll_bar(element) or any(index in scroll_bars for index in node.ancestors):
            scroll_bars.add(node.index)
            continue
        if node.index in stale:
            continue
        if node.role == "other" and not (element.label or element.identifier or element.value):
            continue
        frame = element.frame
        frame_text = "" if frame is None else ",".join(str(round(v)) for v in frame)
        items.append(
            "\x1f".join(
                (
                    node.role,
                    element.label or "",
                    element.identifier or "",
                    element.value or "",
                    "1" if element.enabled else "0",
                    frame_text,
                )
            )
        )
    return hashlib.sha256("\n".join(sorted(items)).encode("utf-8")).hexdigest()


def subtree_text(element: UIElement) -> str:
    """Label, value and every descendant label and value, space-joined."""
    texts: list[str] = []
    for item in walk([element]):
        for text in (item.label, item.value):
            if text and text not in texts:
                texts.append(text)
    return " ".join(texts)
