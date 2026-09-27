"""Exploratory hunts stay inside step and time budgets and report prototype gaps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import AppCrashedError
from swarmqa.explorer.exploratory import run_exploratory
from swarmqa.models import Shard, UIElement
from swarmqa.testing import make_app, sample_config


def _fingerprint(kind: str, title: str, target: str) -> str:
    payload = f"{kind}\n{title.strip().lower()}\n{target.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _hunt(
    tmp_path: Path,
    *,
    goal: str,
    tree: list[UIElement],
    maturity: str = "shipped",
    max_steps: int = 40,
    max_time_s: float = 120,
    video: str = "always",
    crash: set[str] | None = None,
    timeout: set[str] | None = None,
    name: str = "goal",
    kind: str = "exploratory",
    on_step_failure: str = "stop",
    app_path: str | None = None,
    driver_cls: type[FakeDriver] = FakeDriver,
):
    app = make_app(tmp_path)
    app.maturity = maturity  # type: ignore[assignment]
    if app_path is not None:
        app.path = app_path
    config = sample_config(app)
    config.explorer.max_steps = max_steps
    config.explorer.max_time_s = max_time_s
    config.explorer.on_step_failure = on_step_failure  # type: ignore[assignment]
    config.video.mode = video  # type: ignore[assignment]
    root = tmp_path / "reports" / name
    work = root / "workers" / "w1"
    driver = driver_cls(app, work)
    driver.set_tree(tree)
    if crash:
        driver.crash_labels.update(crash)
    if timeout:
        driver.timeout_labels.update(timeout)
    shard = Shard(id="s-1-goal", kind=kind, name=name, goal=goal)  # type: ignore[arg-type]
    result = run_exploratory(shard, driver, config, worker_id="w1", work_dir=work)
    return result, driver, root


def _clicks(driver: FakeDriver) -> list[str]:
    return [action for action in driver.actions_log if action.startswith("click:")]


def _buttons(*labels: str) -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Sample",
            children=[UIElement(role="button", label=label) for label in labels],
        )
    ]


def test_prototype_missing_control_is_a_finding(tmp_path: Path):
    tree = [
        UIElement(
            role="window",
            label="Sample",
            children=[
                UIElement(role="button", label="Save"),
                UIElement(role="textfield", label="Email", value=""),
            ],
        )
    ]
    result, driver, root = _hunt(
        tmp_path,
        goal="Open the Settings gear",
        tree=tree,
        maturity="prototype",
    )
    assert result.status == "failed"
    assert result.error is None
    missing = [finding for finding in result.findings if finding.kind == "missing_control"]
    assert missing
    finding = missing[0]
    assert finding.severity == "high"
    assert finding.details == "Settings gear missing — trying menu bar"
    assert finding.worker_id == "w1"
    assert finding.backend == "local"
    assert finding.environment["bundle_id"] == "dev.swarmqa.sample"
    assert finding.fingerprint == _fingerprint(finding.kind, finding.title, "Settings, gear")
    assert "menu:Settings" in driver.actions_log
    assert "click:Save" not in driver.actions_log
    assert "launch" in driver.actions_log

    markdown = (root / "findings" / f"{finding.id}.md").read_text(encoding="utf-8")
    assert finding.title in markdown
    assert "Severity: high" in markdown
    assert "Worker: w1" in markdown
    assert "Backend: local" in markdown
    assert finding.details in markdown
    replay_path = root / "findings" / f"{finding.id}.replay.json"
    assert replay_path.is_file()
    assert finding.replay_json == f"findings/{finding.id}.replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    assert replay["version"] == 1
    assert replay["steps"][0] == {"action": "launch"}
    assert {"action": "menu", "path": ["Settings"]} in replay["steps"]
    assert any(step["action"] == "menu" and step["path"] == ["gear"] for step in replay["steps"])
    assert finding.screenshots
    shot = root / finding.screenshots[0]
    assert shot.is_file()
    assert not finding.screenshots[0].startswith("/")


def test_crash_label_produces_crash_finding(tmp_path: Path):
    result, driver, root = _hunt(
        tmp_path,
        goal="Save and Cancel",
        tree=_buttons("Save", "Cancel"),
        crash={"Save"},
    )
    crashes = [finding for finding in result.findings if finding.kind == "crash"]
    assert len(crashes) == 1
    finding = crashes[0]
    assert result.status == "failed"
    assert result.error is None
    assert finding.severity == "critical"
    assert finding.details == "Save crashed the app"
    assert finding.fingerprint == _fingerprint("crash", "Crash on Save", "Save")
    replay = json.loads((root / "findings" / f"{finding.id}.replay.json").read_text(encoding="utf-8"))
    assert {
        "action": "click",
        "target": {"role": "button", "label": "Save"},
    } in replay["steps"]
    assert "click:Save" not in driver.actions_log
    assert "click:Cancel" not in driver.actions_log


def test_max_steps_stops_the_hunt(tmp_path: Path):
    labels = ("Alpha", "Beta", "Gamma", "Delta")
    tight, tight_driver, _root = _hunt(
        tmp_path,
        goal="Alpha Beta Gamma Delta",
        tree=_buttons(*labels),
        max_steps=2,
        name="tight",
    )
    assert _clicks(tight_driver) == []
    assert len(tight.steps) <= 2
    assert "launch" in tight_driver.actions_log
    assert tight.status == "passed"
    assert tight.findings == []

    roomy, roomy_driver, _root = _hunt(
        tmp_path,
        goal="Alpha Beta Gamma Delta",
        tree=_buttons(*labels),
        max_steps=40,
        name="roomy",
    )
    assert _clicks(roomy_driver) == [f"click:{label}" for label in labels]
    assert len(roomy.steps) <= 40

    one, one_driver, _root = _hunt(
        tmp_path,
        goal="Alpha Beta Gamma Delta",
        tree=_buttons(*labels),
        max_steps=3,
        name="one",
    )
    assert _clicks(one_driver) == ["click:Alpha"]
    assert len(one.steps) <= 3


def test_max_time_stops_the_hunt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    labels = ("Alpha", "Beta", "Gamma", "Delta")
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        if calls["n"] >= 4:
            return 10_000.0
        return 0.0

    monkeypatch.setattr("swarmqa.explorer.exploratory._monotonic", clock)
    stopped, driver, _root = _hunt(
        tmp_path,
        goal="Alpha Beta Gamma Delta",
        tree=_buttons(*labels),
        max_time_s=5,
        name="timed",
    )
    assert "launch" in driver.actions_log
    assert _clicks(driver) == []
    assert calls["n"] >= 4
    assert len(stopped.steps) <= 40

    monkeypatch.undo()
    _full, full_driver, _root = _hunt(
        tmp_path,
        goal="Alpha Beta Gamma Delta",
        tree=_buttons(*labels),
        max_time_s=120,
        name="untimed",
    )
    assert _clicks(full_driver) == [f"click:{label}" for label in labels]


def test_error_and_empty_states(tmp_path: Path):
    tree = [
        UIElement(role="label", label="Error banner"),
        UIElement(role="textfield", label="Inbox", value="empty"),
        UIElement(role="textfield", label="Email", value=""),
    ]
    result, _driver, _root = _hunt(
        tmp_path,
        goal="Check Inbox",
        tree=tree,
        maturity="prototype",
    )
    kinds = {finding.kind: finding for finding in result.findings}
    assert "error_state" in kinds or any(item.kind == "error_state" for item in result.findings)
    error_findings = [item for item in result.findings if item.kind == "error_state"]
    titles = {item.title for item in error_findings}
    assert "Error state: Error banner" in titles
    assert "Error state: Inbox" in titles
    assert all(item.severity == "medium" for item in error_findings)
    assert any("error state" in item.details for item in error_findings)
    assert any("empty state" in item.details for item in error_findings)
    assert result.status == "failed"
    assert not any(item.kind == "missing_control" for item in result.findings)
    assert not any(item.title.startswith("Error state: Email") for item in result.findings)


def test_timeout_is_unresponsive(tmp_path: Path):
    result, _driver, _root = _hunt(
        tmp_path,
        goal="Press Wait",
        tree=_buttons("Wait"),
        timeout={"Wait"},
    )
    finding = next(item for item in result.findings if item.kind == "unresponsive")
    assert result.status == "failed"
    assert finding.severity == "high"
    assert finding.details == "Wait timed out — control unresponsive"
    assert result.error is None


def test_shipped_missing_control_is_not_a_finding(tmp_path: Path):
    result, driver, _root = _hunt(
        tmp_path,
        goal="Open Settings",
        tree=_buttons("Save"),
        maturity="shipped",
    )
    assert result.findings == []
    assert result.status == "passed"
    assert result.error is None
    assert "menu:Settings" in driver.actions_log


def test_goal_reached_passes_without_findings(tmp_path: Path):
    tree = [
        UIElement(
            role="window",
            label="Sample",
            children=[
                UIElement(role="button", label="Save"),
                UIElement(role="textfield", label="Email", value=""),
            ],
        )
    ]
    result, driver, _root = _hunt(tmp_path, goal="Save", tree=tree, video="always")
    assert result.status == "passed"
    assert result.findings == []
    assert result.error is None
    assert result.shard_id == "s-1-goal"
    assert result.shard_kind == "exploratory"
    assert result.worker_id == "w1"
    assert result.backend == "local"
    assert "click:Save" in driver.actions_log
    assert driver.video_path is not None
    assert driver.video_path.is_file()


def test_nested_goal_button_is_clicked(tmp_path: Path):
    result, driver, _root = _hunt(
        tmp_path,
        goal="Open Settings",
        tree=_buttons("Settings"),
        maturity="prototype",
    )
    assert result.status == "passed"
    assert result.findings == []
    assert "click:Settings" in driver.actions_log


def test_disabled_button_is_present_and_not_clicked(tmp_path: Path):
    tree = [UIElement(role="button", label="Save", enabled=False)]
    result, driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=tree,
        maturity="prototype",
    )
    assert _clicks(driver) == []
    assert result.findings == []
    assert result.status == "passed"


def test_menu_path_satisfies_a_menu_only_control(tmp_path: Path):
    tree = [
        UIElement(
            role="menubar",
            label="Menu",
            children=[
                UIElement(
                    role="menu",
                    label="File",
                    children=[UIElement(role="menuitem", label="Export")],
                )
            ],
        )
    ]
    result, driver, _root = _hunt(
        tmp_path,
        goal="Export",
        tree=tree,
        maturity="prototype",
    )
    assert result.status == "passed"
    assert result.findings == []
    assert "menu:File > Export" in driver.actions_log


def test_video_policy(tmp_path: Path):
    passed, passed_driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        video="on_failure",
        name="pass-video",
    )
    assert passed.status == "passed"
    assert passed_driver.video_path is not None
    assert not passed_driver.video_path.exists()
    assert "video:start" in passed_driver.actions_log

    failed, failed_driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        video="on_failure",
        crash={"Save"},
        name="fail-video",
    )
    assert failed.status == "failed"
    assert failed_driver.video_path is not None
    assert failed_driver.video_path.is_file()
    assert failed.findings[0].video == "workers/w1/media/session.mp4"

    explored, explored_driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        video="exploratory_only",
        name="explore-video",
    )
    assert explored.status == "passed"
    assert explored_driver.video_path is not None
    assert explored_driver.video_path.is_file()

    quiet, quiet_driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        video="exploratory_only",
        kind="scripted",
        name="scripted-video",
    )
    assert "video:start" not in quiet_driver.actions_log
    assert quiet.status == "passed"


def test_missing_bundle_is_launch_finding(tmp_path: Path):
    result, driver, root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        app_path=str(tmp_path / "Missing.app"),
    )
    assert result.status == "failed"
    assert result.error is None
    finding = result.findings[0]
    assert finding.kind == "launch"
    assert finding.severity == "critical"
    assert "missing" in finding.details.lower()
    assert "video:start" not in driver.actions_log
    replay = json.loads((root / "findings" / f"{finding.id}.replay.json").read_text(encoding="utf-8"))
    assert replay["steps"][0]["action"] == "launch"


def test_launch_crash_is_critical(tmp_path: Path):
    class CrashOnLaunch(FakeDriver):
        def launch(self) -> None:
            raise AppCrashedError("boom")

    result, _driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        driver_cls=CrashOnLaunch,
    )
    assert result.status == "failed"
    assert result.findings[0].kind == "crash"
    assert result.findings[0].severity == "critical"
    assert result.error is None


def test_stop_policy_ends_the_hunt_on_first_crash(tmp_path: Path):
    result, driver, root = _hunt(
        tmp_path,
        goal="Save Delete Cancel",
        tree=_buttons("Save", "Delete", "Cancel"),
        crash={"Save", "Delete"},
    )
    assert [item.kind for item in result.findings] == ["crash"]
    assert "click:Cancel" not in driver.actions_log
    replay = json.loads(
        (root / "findings" / f"{result.findings[0].id}.replay.json").read_text(encoding="utf-8")
    )
    clicked = [
        step.get("target", {}).get("label")
        for step in replay["steps"]
        if step["action"] == "click"
    ]
    assert clicked == ["Save"]


def test_continue_policy_keeps_hunting_after_crash(tmp_path: Path):
    result, _driver, _root = _hunt(
        tmp_path,
        goal="Save Delete",
        tree=_buttons("Save", "Delete"),
        crash={"Save", "Delete"},
        on_step_failure="continue",
    )
    crashes = [item for item in result.findings if item.kind == "crash"]
    assert len(crashes) == 2
    assert result.status == "failed"


def test_fatal_driver_fault_is_the_only_error_string(tmp_path: Path):
    class Boom(FakeDriver):
        def accessibility_tree(self):
            raise RuntimeError("driver blew up")

    result, _driver, _root = _hunt(
        tmp_path,
        goal="Save",
        tree=_buttons("Save"),
        driver_cls=Boom,
    )
    assert result.status == "error"
    assert result.findings == []
    assert result.error is not None
    assert "driver blew up" in result.error
