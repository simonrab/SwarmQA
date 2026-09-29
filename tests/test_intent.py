from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from swarmqa.errors import IntentError
from swarmqa.intent.ingest import build_queue
from swarmqa.intent.record import record_flow
from swarmqa.models import ElementQuery
from swarmqa.testing import sample_config
from swarmqa.util import slug

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "intents"


def _copy_example(name: str, directory: Path) -> Path:
    target = directory / name
    target.write_text((EXAMPLES / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _config(*paths: Path, **updates):
    config = sample_config()
    config.intents = [str(path) for path in paths]
    for name, value in updates.items():
        setattr(config, name, value)
    return config


def _ids(shards) -> list[str]:
    return [shard.id for shard in shards]


def test_markdown_example_becomes_scripted_and_exploratory(tmp_path: Path):
    path = _copy_example("smoke.md", tmp_path)
    queue = build_queue(_config(path))

    assert [shard.kind for shard in queue] == ["scripted", "exploratory"]
    scripted, explore = queue
    assert scripted.name == "Sign in and toggle dark mode"
    assert scripted.goal == (
        "Sign in and toggle dark mode\n\n"
        "Sign in, open Settings, toggle Dark Mode, confirm the window title updates."
    )
    assert scripted.constraints == ["Stay within Settings", "Finish within 30s"]
    assert scripted.source_path == str(path)
    assert [(step.action, step.target, step.text, step.keys, step.exists) for step in scripted.actions] == [
        ("click", ElementQuery(role="button", label="Sign In"), None, [], None),
        ("type", ElementQuery(label="Email"), "a@b.c", [], None),
        ("key", None, None, ["cmd", "return"], None),
        ("click", ElementQuery(role="button", label="Dark Mode"), None, [], None),
        ("assert", ElementQuery(label="Dark Mode"), None, [], True),
    ]
    assert explore.seed == "explore"
    assert explore.actions == []
    assert explore.goal == scripted.goal
    assert explore.constraints == scripted.constraints
    assert _ids(queue) == [
        f"s-1-{slug(scripted.name)}",
        f"s-2-{slug(explore.name)}",
    ]


def test_markdown_step_grammar(tmp_path: Path):
    path = tmp_path / "grammar.md"
    path.write_text(
        """# Drive the window

Open the document window.

## Steps

- click "Save"
- click button "Save"
- type "hello" into "Email"
- type "hello" into textfield "Email"
- key cmd+shift+return
- menu File > New > Window
- wait for "Email" 5s
- wait for textfield "Email" 1m
- scroll -3
- scroll +2
- screenshot "after"
- assert "Save" exists
- assert button "Save" missing
- relaunch
- launch
""",
        encoding="utf-8",
    )
    config = _config(path)
    config.coverage.exploratory = False
    scripted = build_queue(config)[0]
    actions = scripted.actions
    assert actions[0].target == ElementQuery(label="Save")
    assert actions[1].target == ElementQuery(role="button", label="Save")
    assert actions[2].text == "hello" and actions[2].target == ElementQuery(label="Email")
    assert actions[3].target == ElementQuery(role="textfield", label="Email")
    assert actions[4].keys == ["cmd", "shift", "return"]
    assert actions[5].path == ["File", "New", "Window"]
    assert actions[6].timeout_s == 5
    assert actions[7].timeout_s == 60
    assert actions[8].delta == -3
    assert actions[9].delta == 2
    assert actions[10].name == "after"
    assert actions[11].exists is True
    assert actions[12].exists is False and actions[12].target.role == "button"
    assert actions[13].action == "relaunch"
    assert actions[14].action == "launch"


def test_markdown_without_steps_is_exploratory(tmp_path: Path):
    path = tmp_path / "explore.md"
    path.write_text(
        """# Explore settings

Look around Settings for broken toggles.

## Constraints

- Stay inside Settings
""",
        encoding="utf-8",
    )
    queue = build_queue(_config(path))
    assert len(queue) == 1
    shard = queue[0]
    assert shard.kind == "exploratory"
    assert shard.seed is None
    assert shard.goal == "Explore settings\n\nLook around Settings for broken toggles."
    assert shard.constraints == ["Stay inside Settings"]
    assert shard.id == f"s-1-{slug(shard.name)}"


def test_bad_markdown_step_raises(tmp_path: Path):
    path = tmp_path / "bad.md"
    path.write_text("# Title\n\n## Steps\n\n- hop \"Save\"\n", encoding="utf-8")
    with pytest.raises(IntentError, match="unrecognized step"):
        build_queue(_config(path))


def test_hand_authored_json_loads_without_record_flow(tmp_path: Path):
    path = _copy_example("smoke.json", tmp_path)
    config = _config(path)
    config.coverage.exploratory = True
    queue = build_queue(config)

    assert len(queue) == 1
    shard = queue[0]
    assert shard.kind == "scripted"
    assert shard.name == "toggle-dark-mode"
    assert shard.seed is None
    assert shard.id == "s-1-toggle-dark-mode"
    assert shard.actions[0].target == ElementQuery(role="button", label="Sign In")
    assert shard.actions[1].text == "a@b.c"
    assert shard.actions[1].target == ElementQuery(role="textfield", label="Email")
    assert shard.actions[2].keys == ["cmd", "return"]
    assert shard.actions[4].exists is True
    assert shard.actions[5].name == "after-dark-mode"


def test_rejects_flow_versions_other_than_1(tmp_path: Path):
    for version in (0, 2, "1", 1.5, None):
        path = tmp_path / f"flow-{version}.json"
        path.write_text(
            json.dumps({"version": version, "name": "bad", "steps": []}),
            encoding="utf-8",
        )
        with pytest.raises(IntentError, match="flow version must be 1"):
            build_queue(_config(path))

    missing = tmp_path / "missing-version.json"
    missing.write_text(json.dumps({"name": "bad", "steps": []}), encoding="utf-8")
    with pytest.raises(IntentError, match="flow version must be 1"):
        build_queue(_config(missing))


def test_rejects_unknown_flow_field(tmp_path: Path):
    path = tmp_path / "extra.json"
    path.write_text(
        json.dumps({"version": 1, "name": "extra", "steps": [], "note": "nope"}),
        encoding="utf-8",
    )
    with pytest.raises(IntentError, match="unknown flow field"):
        build_queue(_config(path))


def test_empty_directory_raises(tmp_path: Path):
    empty = tmp_path / "intents"
    empty.mkdir()
    (empty / "notes.txt").write_text("ignore me", encoding="utf-8")
    with pytest.raises(IntentError, match="intent set is empty"):
        build_queue(_config(empty))


def test_missing_intent_path(tmp_path: Path):
    missing = tmp_path / "gone.md"
    with pytest.raises(IntentError, match="intent not found"):
        build_queue(_config(missing))


def test_directory_is_recursive_and_sorted(tmp_path: Path):
    root = tmp_path / "intents"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "b.md").write_text("# Bee\n\nLook around.\n", encoding="utf-8")
    (nested / "a.md").write_text("# Aye\n\nLook around.\n", encoding="utf-8")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    queue = build_queue(_config(root))
    assert [shard.name for shard in queue] == ["Bee", "Aye"]
    assert all(shard.kind == "exploratory" for shard in queue)


def test_mixed_sources_follow_shard_strategy(tmp_path: Path):
    md = _copy_example("smoke.md", tmp_path)
    flow = _copy_example("smoke.json", tmp_path)
    config = _config(md, flow)
    config.suite.command = "xcodebuild test -scheme MyApp"
    config.visual.enabled = True
    config.visual.baseline_dir = str(tmp_path / "missing-baselines")
    config.shard_strategy = "intent"

    intent_order = build_queue(config)
    assert [shard.kind for shard in intent_order] == [
        "scripted",
        "exploratory",
        "scripted",
        "suite",
        "visual",
    ]
    assert intent_order[0].source_path == str(md)
    assert intent_order[2].name == "toggle-dark-mode"
    assert intent_order[3].suite_command == "xcodebuild test -scheme MyApp"
    assert intent_order[4].visual_names == []
    assert _ids(intent_order) == [f"s-{index}-{slug(shard.name)}" for index, shard in enumerate(intent_order, start=1)]

    config.shard_strategy = "suite"
    suite_first = build_queue(config)
    assert [shard.kind for shard in suite_first] == [
        "suite",
        "scripted",
        "exploratory",
        "scripted",
        "visual",
    ]
    assert suite_first[0].id == "s-1-suite"

    config.shard_strategy = "exploratory_seed"
    explore_first = build_queue(config)
    assert [shard.kind for shard in explore_first] == [
        "exploratory",
        "scripted",
        "scripted",
        "suite",
        "visual",
    ]
    assert explore_first[0].seed == "explore"
    assert explore_first[0].id.startswith("s-1-")


def test_coverage_flags_drop_scripted_and_exploratory(tmp_path: Path):
    path = _copy_example("smoke.md", tmp_path)
    flow = _copy_example("smoke.json", tmp_path)

    scripted_only = _config(path)
    scripted_only.coverage.exploratory = False
    queue = build_queue(scripted_only)
    assert [shard.kind for shard in queue] == ["scripted"]

    explore_only = _config(path, flow)
    explore_only.coverage.scripted = False
    queue = build_queue(explore_only)
    assert [shard.kind for shard in queue] == ["exploratory"]
    assert queue[0].seed == "explore"
    assert queue[0].source_path == str(path)

    neither = _config(path, flow)
    neither.coverage.scripted = False
    neither.coverage.exploratory = False
    with pytest.raises(IntentError, match="intent set is empty"):
        build_queue(neither)


def test_suite_command_alone_is_a_queue(tmp_path: Path):
    config = sample_config()
    config.suite.command = "pytest -q"
    queue = build_queue(config)
    assert len(queue) == 1
    assert queue[0].kind == "suite"
    assert queue[0].suite_command == "pytest -q"
    assert queue[0].id == "s-1-suite"


def test_visual_shard_lists_png_stems_once(tmp_path: Path):
    baseline = tmp_path / "baselines"
    nested = baseline / "nested"
    nested.mkdir(parents=True)
    (baseline / "login.png").write_bytes(b"png")
    (baseline / "home.PNG").write_bytes(b"png")
    (baseline / "notes.txt").write_text("nope", encoding="utf-8")
    (nested / "buried.png").write_bytes(b"png")

    config = sample_config()
    config.visual.enabled = True
    config.coverage.visual = True
    config.visual.baseline_dir = str(baseline)
    queue = build_queue(config)
    assert len(queue) == 1
    visual = queue[0]
    assert visual.kind == "visual"
    assert visual.visual_names == ["home", "login"]
    assert visual.id == "s-1-visual"
    assert visual.source_path == str(baseline)


def test_record_flow_events_round_trip(tmp_path: Path):
    out = tmp_path / "flows" / "saved.json"
    events = [
        {"action": "click", "target": {"role": "button", "label": "Sign In"}},
        {"action": "type", "target": {"label": "Email"}, "text": "a@b.c"},
        {"action": "key", "keys": ["cmd", "return"]},
        {"action": "screenshot", "name": "after"},
    ]
    written = record_flow(app_path="/Applications/Sample.app", out_path=out, name="saved", events=events)
    assert written == out
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.startswith('{\n  "version": 1')
    payload = json.loads(text)
    assert payload["version"] == 1
    assert payload["name"] == "saved"
    assert payload["steps"] == events

    config = _config(out)
    config.coverage.exploratory = True
    queue = build_queue(config)
    assert len(queue) == 1
    assert queue[0].name == "saved"
    assert queue[0].actions[1].text == "a@b.c"
    assert queue[0].actions[3].name == "after"


def test_record_flow_default_is_launch_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('click "Save"\n'))
    out = tmp_path / "launch.json"
    record_flow(app_path="Sample.app", out_path=out)
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "version": 1,
        "name": "recorded-flow",
        "steps": [{"action": "launch"}],
    }


def test_record_flow_interactive_stops_at_done_or_eof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('click button "Sign In"\n\nscroll -3\n  done  \nkey cmd+q\n'),
    )
    out = tmp_path / "interactive.json"
    record_flow(app_path="Sample.app", out_path=out, name="interactive", interactive=True)
    steps = json.loads(out.read_text(encoding="utf-8"))["steps"]
    assert steps == [
        {"action": "click", "target": {"role": "button", "label": "Sign In"}},
        {"action": "scroll", "delta": -3},
    ]

    monkeypatch.setattr("sys.stdin", io.StringIO("relaunch\n"))
    eof = tmp_path / "eof.json"
    record_flow(app_path="Sample.app", out_path=eof, interactive=True, name="eof")
    assert json.loads(eof.read_text(encoding="utf-8"))["steps"] == [{"action": "relaunch"}]


def test_record_flow_events_win_over_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('click "Save"\n'))
    out = tmp_path / "events.json"
    record_flow(
        app_path="Sample.app",
        out_path=out,
        interactive=True,
        events=[{"action": "relaunch"}],
    )
    assert json.loads(out.read_text(encoding="utf-8"))["steps"] == [{"action": "relaunch"}]


def test_record_flow_rejects_bad_event(tmp_path: Path):
    out = tmp_path / "bad.json"
    with pytest.raises(IntentError, match="unknown action"):
        record_flow(app_path="Sample.app", out_path=out, events=[{"action": "hop"}])
    assert not out.exists()


def test_tap_point_and_swipe_steps_round_trip_and_replay(tmp_path):
    from swarmqa.driver.fake import FakeDriver
    from swarmqa.intent.ingest import action_from_dict, action_to_dict
    from swarmqa.models import AppTarget, UIElement
    from swarmqa.explorer.scripted import _execute

    tap = action_from_dict({"action": "tap_point", "point": [30, 120]}, origin="t")
    swipe = action_from_dict({"action": "swipe", "point": [200, 600], "end": [200, 200]}, origin="t")
    assert action_to_dict(tap) == {"action": "tap_point", "point": [30.0, 120.0]}
    assert action_from_dict(action_to_dict(swipe), origin="t") == swipe
    for bad in ({"action": "tap_point"}, {"action": "swipe", "point": [1, 2]}, {"action": "tap_point", "point": [1]}):
        with pytest.raises(IntentError):
            action_from_dict(bad, origin="t")

    driver = FakeDriver(AppTarget(path="/A.app"), tmp_path, require_path=False)
    driver.set_tree([
        UIElement(role="button", label="Save As…", frame=(10, 10, 100, 44)),
        UIElement(role="button", label="Save", frame=(10, 100, 100, 44)),
    ])
    driver.transitions["Save"] = []
    driver.launch()
    _execute(driver, tap, 0)
    assert driver.tree == []
    _execute(driver, swipe, 1)
    assert driver.actions_log[-1] == "swipe:200,600->200,200"
