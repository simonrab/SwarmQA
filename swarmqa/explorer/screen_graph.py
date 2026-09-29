"""Screen fingerprints and the screen graph the agent loop builds.

A screen's fingerprint is a hash of its structural skeleton: the roles of
the elements that matter, the identifiers of any element, and the labels of
interactive elements and navigation bars, with digits in labels and
identifiers folded to ``#``. Values, frames, static text and images without
identifiers are left out, so the same screen with different field contents,
times or counters keeps its fingerprint. Siblings that are identical after
masking (list rows such as ``item-17`` and ``item-18``) count once, so a list
growing by a row does not make a new screen.

The graph's nodes are screens and its edges are ``(screen, action) ->
screen``. It is plain JSON on disk and `merge` unions two graphs, so graphs
from several devices can be combined.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from swarmqa.driver.query import walk
from swarmqa.models import ElementQuery, UIElement

GRAPH_VERSION = 1
ControlKind = Literal["tap", "type"]

TAP_ROLES = frozenset(
    {
        "button",
        "link",
        "cell",
        "tab",
        "tabbarbutton",
        "switch",
        "toggle",
        "checkbox",
        "radiobutton",
        "segmentedcontrol",
        "popupbutton",
        "menubutton",
        "menuitem",
        "menubaritem",
        "disclosuretriangle",
        "stepper",
        "incrementor",
        "combobox",
    }
)
TYPE_ROLES = frozenset({"textfield", "securetextfield", "searchfield", "textview", "textarea"})
INTERACTIVE_ROLES = TAP_ROLES | TYPE_ROLES
# Containers whose label names the screen (a navigation bar title).
TITLE_ROLES = frozenset({"navigationbar", "toolbar", "tabbar", "sheet", "alert", "dialog"})
_DIGITS = re.compile(r"\d+")


def normalize_role(role: str) -> str:
    """Lower-case, no spaces or underscores: ``Text Field`` -> ``textfield``."""
    return (role or "").strip().lower().replace(" ", "").replace("_", "")


def _norm_label(text: str | None) -> str:
    return _DIGITS.sub("#", (text or "").strip().lower())


def _signature(element: UIElement) -> str | None:
    """Skeleton text for one element and its children; None when it adds nothing."""
    role = normalize_role(element.role)
    parts: list[str] = []
    for child in element.children:
        sig = _signature(child)
        if sig is not None and sig not in parts:
            parts.append(sig)
    interactive = role in INTERACTIVE_ROLES
    head = role
    if element.identifier:
        # Digits are masked like labels, so rows item-17 and item-18 match.
        head += f"#{_DIGITS.sub('#', element.identifier)}"
    if interactive or role in TITLE_ROLES:
        head += f"[{_norm_label(element.label)}]"
    if not parts and not interactive and not element.identifier and role not in TITLE_ROLES:
        return None
    return head + ("(" + ",".join(parts) + ")" if parts else "")


def skeleton(tree: list[UIElement]) -> str:
    """The text `fingerprint` hashes. Useful when debugging a collision."""
    parts: list[str] = []
    for element in tree:
        sig = _signature(element)
        if sig is not None and sig not in parts:
            parts.append(sig)
    return ",".join(parts)


def fingerprint(tree: list[UIElement]) -> str:
    """Stable 12-hex screen id from the structural skeleton of `tree`."""
    return hashlib.sha256(skeleton(tree).encode("utf-8")).hexdigest()[:12]


@dataclass
class Control:
    """One actionable element on a screen. `key` is ``role|label|identifier``."""

    key: str
    role: str
    label: str = ""
    identifier: str | None = None
    kind: ControlKind = "tap"

    def query(self) -> ElementQuery:
        return ElementQuery(role=self.role, label=self.label or None, identifier=self.identifier)

    def display(self) -> str:
        return self.label or self.identifier or self.role


def control_key(element: UIElement) -> str:
    return f"{element.role}|{element.label}|{element.identifier or ''}"


def actionable_controls(tree: list[UIElement]) -> list[Control]:
    """Enabled interactive elements with a label or identifier, in tree order, once each."""
    found: list[Control] = []
    seen: set[str] = set()
    for element in walk(tree):
        role = normalize_role(element.role)
        if role not in INTERACTIVE_ROLES or not element.enabled:
            continue
        if not (element.label or element.identifier):
            continue
        key = control_key(element)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            Control(
                key=key,
                role=element.role,
                label=element.label or "",
                identifier=element.identifier,
                kind="type" if role in TYPE_ROLES else "tap",
            )
        )
    return found


def action_key(action: dict[str, Any]) -> str:
    """Canonical edge key for a JSON action (``{"kind": "tap", "control": key}``)."""
    return json.dumps(action, sort_keys=True, separators=(",", ":"))


@dataclass
class ScreenNode:
    id: str
    controls: list[Control] = field(default_factory=list)
    tried: set[str] = field(default_factory=set)
    screenshot: str | None = None
    visits: int = 0
    order: int = 0

    def untried(self) -> list[Control]:
        return [control for control in self.controls if control.key not in self.tried]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "controls": [
                {
                    "key": c.key,
                    "role": c.role,
                    "label": c.label,
                    "identifier": c.identifier,
                    "kind": c.kind,
                }
                for c in self.controls
            ],
            "tried": sorted(self.tried),
            "screenshot": self.screenshot,
            "visits": self.visits,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScreenNode":
        return cls(
            id=data["id"],
            controls=[Control(**item) for item in data.get("controls", [])],
            tried=set(data.get("tried", [])),
            screenshot=data.get("screenshot"),
            visits=int(data.get("visits", 0)),
            order=int(data.get("order", 0)),
        )


@dataclass
class Edge:
    """Doing `action` on screen `source` led to screen `target`."""

    source: str
    action: dict[str, Any]
    target: str
    count: int = 1

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, action_key(self.action))

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "action": self.action, "target": self.target, "count": self.count}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Edge":
        return cls(
            source=data["source"],
            action=dict(data["action"]),
            target=data["target"],
            count=int(data.get("count", 1)),
        )


class ScreenGraph:
    """Screens seen, controls tried, and which action led where."""

    def __init__(self) -> None:
        self.start: str | None = None
        self.nodes: dict[str, ScreenNode] = {}
        self.edges: dict[tuple[str, str], Edge] = {}

    # Building -------------------------------------------------------------

    def add_screen(
        self,
        screen_id: str,
        controls: list[Control],
        screenshot: str | Path | None = None,
    ) -> bool:
        """Record a visit. Return True the first time `screen_id` is seen."""
        node = self.nodes.get(screen_id)
        if node is None:
            node = ScreenNode(
                id=screen_id,
                controls=list(controls),
                screenshot=str(screenshot) if screenshot is not None else None,
                order=len(self.nodes),
            )
            self.nodes[screen_id] = node
            if self.start is None:
                self.start = screen_id
            node.visits = 1
            return True
        node.visits += 1
        known = {control.key for control in node.controls}
        node.controls.extend(control for control in controls if control.key not in known)
        if node.screenshot is None and screenshot is not None:
            node.screenshot = str(screenshot)
        return False

    def mark_tried(self, screen_id: str, key: str) -> None:
        node = self.nodes.get(screen_id)
        if node is not None:
            node.tried.add(key)

    def add_edge(self, source: str, action: dict[str, Any], target: str) -> Edge:
        """Record that `action` on `source` led to `target`. The latest target wins."""
        edge = Edge(source=source, action=dict(action), target=target)
        existing = self.edges.get(edge.key)
        if existing is not None:
            existing.count += 1
            existing.target = target
            return existing
        self.edges[edge.key] = edge
        return edge

    def drop_edge(self, source: str, action: dict[str, Any]) -> None:
        self.edges.pop((source, action_key(action)), None)

    # Queries --------------------------------------------------------------

    def untried(self, screen_id: str) -> list[Control]:
        node = self.nodes.get(screen_id)
        return node.untried() if node is not None else []

    def neighbours(self, screen_id: str) -> list[Edge]:
        """Outgoing edges that change screen, in insertion order."""
        return [e for e in self.edges.values() if e.source == screen_id and e.target != screen_id]

    def _bfs(self, source: str) -> dict[str, list[Edge]]:
        paths: dict[str, list[Edge]] = {source: []}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for edge in self.neighbours(current):
                if edge.target in paths:
                    continue
                paths[edge.target] = paths[current] + [edge]
                queue.append(edge.target)
        return paths

    def untried_frontier(self, source: str | None = None) -> list[str]:
        """Screens with untried controls, breadth-first from `source` (default: start).

        Screens not reachable through known edges come last, in discovery order.
        """
        origin = source or self.start
        ordered: list[str] = []
        if origin is not None and origin in self.nodes:
            ordered = list(self._bfs(origin))
        seen = set(ordered)
        ordered += [n.id for n in sorted(self.nodes.values(), key=lambda n: n.order) if n.id not in seen]
        return [sid for sid in ordered if self.nodes[sid].untried()]

    def path_to(self, screen_id: str, source: str | None = None) -> list[Edge] | None:
        """Shortest known edge path from `source` (default: start) to `screen_id`.

        Empty when they are the same screen; None when no known path exists.
        """
        origin = source or self.start
        if origin is None:
            return None
        return self._bfs(origin).get(screen_id)

    def nearest_frontier(
        self, source: str, *, skip: set[str] | frozenset[str] = frozenset()
    ) -> tuple[str, list[Edge]] | None:
        """Closest screen with untried controls reachable from `source` by known edges."""
        for sid, path in self._bfs(source).items():
            if sid not in skip and self.nodes[sid].untried():
                return sid, path
        return None

    def coverage(self) -> dict[str, Any]:
        controls = sum(len(n.controls) for n in self.nodes.values())
        tried = sum(len(n.tried & {c.key for c in n.controls}) for n in self.nodes.values())
        return {
            "screens": len(self.nodes),
            "edges": len(self.edges),
            "controls": controls,
            "tried": tried,
            "untried": controls - tried,
            "ratio": (tried / controls) if controls else 1.0,
            "frontier": len(self.untried_frontier()),
        }

    # Persistence and merging ---------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": GRAPH_VERSION,
            "start": self.start,
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda n: n.order)],
            "edges": [e.to_dict() for e in self.edges.values()],
            "coverage": self.coverage(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScreenGraph":
        graph = cls()
        graph.start = data.get("start")
        for item in data.get("nodes", []):
            node = ScreenNode.from_dict(item)
            graph.nodes[node.id] = node
        for item in data.get("edges", []):
            edge = Edge.from_dict(item)
            graph.edges[edge.key] = edge
        return graph

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "ScreenGraph":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def merge(self, other: "ScreenGraph") -> "ScreenGraph":
        """Union `other` into this graph in place and return self.

        Controls and tried sets are unioned, visits and edge counts summed, and
        the first screenshot kept. On an edge conflict this graph's target wins.
        """
        if self.start is None:
            self.start = other.start
        for node in sorted(other.nodes.values(), key=lambda n: n.order):
            mine = self.nodes.get(node.id)
            if mine is None:
                copy = ScreenNode.from_dict(node.to_dict())
                copy.order = len(self.nodes)
                self.nodes[node.id] = copy
                continue
            known = {c.key for c in mine.controls}
            mine.controls.extend(Control(**vars(c)) for c in node.controls if c.key not in known)
            mine.tried |= node.tried
            mine.visits += node.visits
            if mine.screenshot is None:
                mine.screenshot = node.screenshot
        for key, edge in other.edges.items():
            mine_edge = self.edges.get(key)
            if mine_edge is None:
                self.edges[key] = Edge.from_dict(edge.to_dict())
            else:
                mine_edge.count += edge.count
        return self
