"""Screen fingerprints and the screen graph."""

from __future__ import annotations

from swarmqa.explorer.screen_graph import (
    ScreenGraph,
    actionable_controls,
    fingerprint,
)
from swarmqa.models import UIElement


def _settings(value: str = "", clock: str = "12:30", badge: str = "3 new") -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Settings",
            frame=(0, 0, 390, 844),
            children=[
                UIElement(role="navigationbar", label="Settings", children=[UIElement(role="button", label="Back", identifier="back")]),
                UIElement(role="text", label=f"Last synced {clock}"),
                UIElement(role="textfield", label="Name", value=value, frame=(10, 100, 300, 40)),
                UIElement(role="button", label=f"Inbox {badge}", frame=(10, 160, 300, 40)),
                UIElement(role="switch", label="Dark Mode", value="0"),
            ],
        )
    ]


def _home() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Settings",
            children=[UIElement(role="button", label="Settings"), UIElement(role="button", label="Profile")],
        )
    ]


def test_fingerprint_ignores_values_frames_and_dynamic_text():
    first = _settings()
    second = _settings(value="Ada Lovelace", clock="09:05", badge="12 new")
    second[0].frame = (0, 0, 1024, 768)
    second[0].children[4].value = "1"
    assert fingerprint(first) == fingerprint(second)


def test_fingerprint_tells_screens_apart():
    assert fingerprint(_settings()) != fingerprint(_home())
    with_alert = _settings()
    with_alert.append(UIElement(role="alert", label="Delete?", children=[UIElement(role="button", label="OK")]))
    assert fingerprint(with_alert) != fingerprint(_settings())
    extra_button = _home()
    extra_button[0].children.append(UIElement(role="button", label="Help"))
    assert fingerprint(extra_button) != fingerprint(_home())


def test_fingerprint_collapses_repeated_rows():
    def listing(rows: int) -> list[UIElement]:
        return [
            UIElement(
                role="table",
                children=[
                    UIElement(role="cell", identifier="row", children=[UIElement(role="text", label=f"Item {i}")])
                    for i in range(rows)
                ],
            )
        ]

    assert fingerprint(listing(2)) == fingerprint(listing(7))


def test_fingerprint_masks_numbered_row_identifiers():
    def listing(rows: int, *, screen: str = "Inbox") -> list[UIElement]:
        return [
            UIElement(
                role="window",
                children=[
                    UIElement(role="navigationbar", label=screen),
                    UIElement(
                        role="table",
                        children=[
                            UIElement(role="cell", identifier=f"item-{i}", label=f"Message {i}")
                            for i in range(17, 17 + rows)
                        ],
                    ),
                ],
            )
        ]

    assert fingerprint(listing(1)) == fingerprint(listing(2)) == fingerprint(listing(9))
    assert fingerprint(listing(2)) != fingerprint(listing(2, screen="Archive"))
    detail = [UIElement(role="window", children=[UIElement(role="button", identifier="reply-1", label="Reply")])]
    assert fingerprint(detail) != fingerprint(listing(1))


def test_actionable_controls_skip_disabled_and_unlabeled():
    tree = [
        UIElement(
            role="window",
            children=[
                UIElement(role="button", label="Go"),
                UIElement(role="button", label="Off", enabled=False),
                UIElement(role="button"),
                UIElement(role="text", label="Hello"),
                UIElement(role="textfield", label="Email"),
                UIElement(role="button", label="Go"),
            ],
        )
    ]
    controls = actionable_controls(tree)
    assert [(c.label, c.kind) for c in controls] == [("Go", "tap"), ("Email", "type")]


def _graph() -> ScreenGraph:
    graph = ScreenGraph()
    home = actionable_controls(_home())
    graph.add_screen("home", home)
    graph.add_screen("settings", actionable_controls(_settings()))
    graph.add_screen("profile", actionable_controls([UIElement(role="button", label="Save")]))
    graph.add_screen("about", actionable_controls([UIElement(role="button", label="Licenses")]))
    graph.add_edge("home", {"kind": "tap", "target": {"label": "Settings"}}, "settings")
    graph.add_edge("home", {"kind": "tap", "target": {"label": "Profile"}}, "profile")
    graph.add_edge("settings", {"kind": "tap", "target": {"label": "About"}}, "about")
    graph.add_edge("settings", {"kind": "back"}, "home")
    return graph


def test_frontier_is_breadth_first_and_shrinks_as_controls_are_tried():
    graph = _graph()
    assert graph.start == "home"
    assert graph.untried_frontier() == ["home", "settings", "profile", "about"]
    for control in graph.untried("home"):
        graph.mark_tried("home", control.key)
    assert graph.untried_frontier() == ["settings", "profile", "about"]
    graph.mark_tried("profile", graph.untried("profile")[0].key)
    assert graph.untried_frontier() == ["settings", "about"]
    assert graph.nearest_frontier("about") == ("about", [])
    graph.mark_tried("about", graph.untried("about")[0].key)
    assert graph.nearest_frontier("about") is None
    target, path = graph.nearest_frontier("home")
    assert target == "settings" and len(path) == 1


def test_path_to_follows_known_edges():
    graph = _graph()
    path = graph.path_to("about")
    assert [edge.action for edge in path] == [
        {"kind": "tap", "target": {"label": "Settings"}},
        {"kind": "tap", "target": {"label": "About"}},
    ]
    assert graph.path_to("home") == []
    assert graph.path_to("home", source="about") is None
    assert [e.target for e in graph.path_to("profile", source="settings")] == ["home", "profile"]


def test_self_loops_are_not_routes_and_latest_target_wins():
    graph = _graph()
    graph.add_edge("settings", {"kind": "tap", "target": {"label": "About"}}, "settings")
    assert graph.path_to("about") is None
    edge = graph.edges[("settings", '{"kind":"tap","target":{"label":"About"}}')]
    assert edge.count == 2


def test_json_round_trip(tmp_path):
    graph = _graph()
    graph.mark_tried("home", graph.untried("home")[0].key)
    path = graph.save(tmp_path / "screen_graph.json")
    loaded = ScreenGraph.load(path)
    assert loaded.to_dict() == graph.to_dict()
    assert loaded.untried_frontier() == graph.untried_frontier()
    assert loaded.coverage()["tried"] == 1


def test_merge_unions_screens_edges_and_tried_controls():
    left = _graph()
    right = ScreenGraph()
    right.add_screen("home", actionable_controls(_home()) + actionable_controls([UIElement(role="button", label="Help")]))
    right.mark_tried("home", right.untried("home")[0].key)
    right.add_screen("help", [])
    right.add_edge("home", {"kind": "tap", "target": {"label": "Help"}}, "help")
    right.add_edge("home", {"kind": "tap", "target": {"label": "Settings"}}, "settings")
    left.merge(right)
    assert set(left.nodes) == {"home", "settings", "profile", "about", "help"}
    assert [c.label for c in left.nodes["home"].controls] == ["Settings", "Profile", "Help"]
    assert len(left.nodes["home"].tried) == 1
    assert left.nodes["home"].visits == 2
    assert left.path_to("help") is not None
    assert left.edges[("home", '{"kind":"tap","target":{"label":"Settings"}}')].count == 2
    # Merging is serialisable both ways.
    assert ScreenGraph.from_dict(left.to_dict()).to_dict() == left.to_dict()


def test_coverage_counts():
    graph = _graph()
    stats = graph.coverage()
    assert stats["screens"] == 4
    assert stats["edges"] == 4
    assert stats["tried"] == 0 and stats["ratio"] == 0
