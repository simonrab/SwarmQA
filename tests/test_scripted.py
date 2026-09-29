"""Scripted explorer: step results, findings, and evidence packs."""

from __future__ import annotations

import json
from pathlib import Path

from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import AppCrashedError
from swarmqa.explorer.scripted import run_scripted
from swarmqa.models import Action, ElementQuery
from swarmqa.report.layout import ensure_campaign_layout, worker_dir
from swarmqa.reporter.findings import fingerprint_for
from swarmqa.testing import make_app, sample_config, sample_tree, scripted_shard

_STEP_KEYS = {
    "action",
    "target",
    "text",
    "keys",
    "delta",
    "path",
    "timeout_s",
    "name",
    "exists",
}


def _campaign(tmp_path: Path, worker_id: str = "w1"):
    root = tmp_path / "reports" / "camp"
    ensure_campaign_layout(root)
    return root, worker_dir(root, worker_id)


def test_missing_control_writes_finding_pack(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.app.maturity = "prototype"
    root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [
            Action(action="click", target=ElementQuery(role="button", label="Save")),
            Action(action="click", target=ElementQuery(role="button", label="Not There")),
            Action(action="click", target=ElementQuery(role="button", label="Dark Mode")),
        ]
    )

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "failed"
    assert result.worker_id == "w1"
    assert result.shard_id == shard.id
    assert result.backend == "local"
    assert [step.status for step in result.steps] == ["passed", "failed", "skipped"]
    assert "click:Save" in driver.actions_log
    assert "click:Dark Mode" not in driver.actions_log
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.kind == "missing_control"
    assert finding.severity == "high"
    assert finding.title == "Missing control: Not There"
    assert finding.worker_id == "w1"
    assert finding.backend == "local"
    assert finding.fingerprint == fingerprint_for(finding.kind, finding.title, "Not There")
    assert finding.environment == {
        "path": app.path,
        "bundle_id": "dev.swarmqa.sample",
        "version": "1.0.0-fake",
    }
    assert finding.steps == ['click "Save"', 'click "Not There"']
    assert finding.screenshots == ["workers/w1/media/failure-1.png"]
    assert finding.video == "workers/w1/media/session.mp4"
    assert finding.replay_json == f"findings/{finding.id}.replay.json"

    shot = root / finding.screenshots[0]
    assert shot.is_file()
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert (root / finding.video).is_file()

    markdown = (root / "findings" / f"{finding.id}.md").read_text(encoding="utf-8")
    assert finding.title in markdown
    assert "high" in markdown
    assert "w1" in markdown
    assert "local" in markdown
    assert finding.screenshots[0] in markdown
    assert finding.replay_json in markdown
    assert finding.video in markdown
    for step in finding.steps:
        assert step in markdown
    assert "1.0.0-fake" in markdown

    replay = json.loads((root / finding.replay_json).read_text(encoding="utf-8"))
    assert replay["version"] == 1
    assert set(replay) <= {"version", "name", "steps"}
    assert [step["action"] for step in replay["steps"]] == ["click", "click"]
    assert replay["steps"][0]["target"]["label"] == "Save"
    assert replay["steps"][-1]["target"]["label"] == "Not There"
    for step in replay["steps"]:
        assert set(step) <= _STEP_KEYS
        assert "action" in step


def test_shipped_missing_control_is_medium(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    root, work = _campaign(tmp_path, "w2")
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [Action(action="click", target=ElementQuery(role="button", label="Not There"))]
    )

    result = run_scripted(shard, driver, config, worker_id="w2", work_dir=work)

    assert result.findings[0].kind == "missing_control"
    assert result.findings[0].severity == "medium"


def test_clean_script_passes_without_findings(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [
            Action(action="click", target=ElementQuery(role="button", label="Save")),
            Action(action="type", target=ElementQuery(role="textfield", label="email"), text="a@b.c"),
            Action(action="key", keys=["cmd", "return"]),
            Action(action="scroll", delta=-3),
            Action(action="menu", path=["File", "New"]),
            Action(action="wait", target=ElementQuery(role="button", label="Save"), timeout_s=1),
            Action(action="screenshot", name="after"),
            Action(action="assert", target=ElementQuery(role="button", label="Save"), exists=True),
            Action(
                action="assert",
                target=ElementQuery(role="button", label="Missing Gear"),
                exists=False,
            ),
            Action(action="relaunch"),
        ]
    )

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "passed"
    assert result.findings == []
    assert [step.status for step in result.steps] == ["passed"] * len(shard.actions)
    assert "type:Email:a@b.c" in driver.actions_log
    assert "key:cmd+return" in driver.actions_log
    assert "scroll:-3" in driver.actions_log
    assert "menu:File > New" in driver.actions_log
    assert "screenshot:after" in driver.actions_log
    assert "relaunch" in driver.actions_log
    assert (work / "media" / "after.png").is_file()
    assert (work / "media" / "session.mp4").is_file()
    assert list((root / "findings").glob("*.md")) == []


def test_assert_exists_failure_is_medium_assertion(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.app.maturity = "prototype"
    _root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [
            Action(action="assert", target=ElementQuery(role="button", label="Not There"), exists=True),
            Action(action="assert", target=ElementQuery(role="button", label="Save"), exists=False),
        ]
    )

    missing = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert missing.status == "failed"
    assert missing.findings[0].kind == "assertion"
    assert missing.findings[0].severity == "medium"
    assert missing.steps[0].status == "failed"
    assert missing.steps[1].status == "skipped"

    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    present = scripted_shard(
        [Action(action="assert", target=ElementQuery(role="button", label="Save"), exists=False)]
    )
    failed_present = run_scripted(present, driver, config, worker_id="w1", work_dir=work)
    assert failed_present.findings[0].kind == "assertion"
    assert "absent" in failed_present.findings[0].details


def test_crash_is_critical_and_timeout_is_high(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    _root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    driver.crash_labels.add("Save")
    crashed = run_scripted(
        scripted_shard([Action(action="click", target=ElementQuery(role="button", label="Save"))]),
        driver,
        config,
        worker_id="w1",
        work_dir=work,
    )
    assert crashed.findings[0].kind == "crash"
    assert crashed.findings[0].severity == "critical"

    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    driver.timeout_labels.add("Email")
    timed_out = run_scripted(
        scripted_shard(
            [Action(action="wait", target=ElementQuery(role="textfield", label="Email"), timeout_s=2)]
        ),
        driver,
        config,
        worker_id="w1",
        work_dir=work,
    )
    assert timed_out.findings[0].kind == "timeout"
    assert timed_out.findings[0].severity == "high"
    assert timed_out.findings[0].screenshots


def test_continue_runs_later_steps(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.explorer.on_step_failure = "continue"
    _root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [
            Action(action="click", target=ElementQuery(role="button", label="Not There")),
            Action(action="click", target=ElementQuery(role="button", label="Save")),
        ]
    )

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "failed"
    assert [step.status for step in result.steps] == ["failed", "passed"]
    assert len(result.findings) == 1
    assert "click:Save" in driver.actions_log


def test_launch_failure_is_critical_launch_finding(tmp_path: Path):
    app = make_app(tmp_path)
    app.path = str(tmp_path / "Missing.app")
    config = sample_config(app)
    root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    shard = scripted_shard(
        [Action(action="click", target=ElementQuery(role="button", label="Save"))]
    )

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.status == "failed"
    assert result.steps[0].status == "skipped"
    finding = result.findings[0]
    assert finding.kind == "launch"
    assert finding.severity == "critical"
    assert finding.environment["path"] == app.path
    assert "video:start" not in driver.actions_log
    replay = json.loads((root / finding.replay_json).read_text(encoding="utf-8"))
    assert replay["version"] == 1
    assert replay["steps"] == [{"action": "launch"}]
    assert (root / "findings" / f"{finding.id}.md").is_file()


def test_crash_during_launch_stays_kind_launch(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    _root, work = _campaign(tmp_path)

    class Boom(FakeDriver):
        def launch(self) -> None:
            raise AppCrashedError("exploded during launch")

    result = run_scripted(
        scripted_shard(),
        Boom(app, work),
        config,
        worker_id="w1",
        work_dir=work,
    )
    assert result.findings[0].kind == "launch"
    assert result.findings[0].severity == "critical"
    assert "exploded during launch" in result.findings[0].details


def test_on_failure_video_is_deleted_after_a_pass(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.video.mode = "on_failure"
    _root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())

    result = run_scripted(scripted_shard(), driver, config, worker_id="w1", work_dir=work)

    assert result.status == "passed"
    assert "video:start" in driver.actions_log
    assert not (work / "media" / "session.mp4").exists()


def test_on_failure_video_is_kept_after_a_failure(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.video.mode = "on_failure"
    root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [Action(action="click", target=ElementQuery(role="button", label="Not There"))]
    )

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.findings[0].video == "workers/w1/media/session.mp4"
    assert (root / result.findings[0].video).is_file()


def test_exploratory_only_does_not_record_scripted_video(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.video.mode = "exploratory_only"
    _root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())

    result = run_scripted(scripted_shard(), driver, config, worker_id="w1", work_dir=work)

    assert result.status == "passed"
    assert "video:start" not in driver.actions_log
    assert not (work / "media" / "session.mp4").exists()


def test_paths_are_absolute_outside_a_campaign_layout(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.app.maturity = "prototype"
    work = tmp_path / "loose"
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [Action(action="click", target=ElementQuery(role="button", label="Not There"))]
    )

    result = run_scripted(shard, driver, config, worker_id="w9", work_dir=work)

    finding = result.findings[0]
    assert Path(finding.screenshots[0]).is_absolute()
    assert Path(finding.replay_json).is_absolute()
    assert Path(finding.screenshots[0]).is_file()
    assert Path(finding.replay_json).is_file()
    assert (work / "findings" / f"{finding.id}.md").is_file()


def test_screenshot_action_is_attached_to_a_later_finding(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    _root, work = _campaign(tmp_path)
    driver = FakeDriver(app, work)
    driver.set_tree(sample_tree())
    shard = scripted_shard(
        [
            Action(action="screenshot", name="before"),
            Action(action="click", target=ElementQuery(role="button", label="Not There")),
        ]
    )

    result = run_scripted(shard, driver, config, worker_id="w1", work_dir=work)

    assert result.findings[0].screenshots == [
        "workers/w1/media/before.png",
        "workers/w1/media/failure-1.png",
    ]


def test_crash_right_after_the_last_step_fails_the_shard(tmp_path: Path):
    """A live driver can report a tap as done and then the app dies."""

    class CrashAfterClick(FakeDriver):
        crashed = False

        def click(self, target):
            super().click(target)
            self.crashed = True

        def accessibility_tree(self):
            if self.crashed:
                raise AppCrashedError("app is no longer running")
            return super().accessibility_tree()

    app = make_app(tmp_path)
    config = sample_config(app)
    _root, work = _campaign(tmp_path)
    driver = CrashAfterClick(app, work)
    driver.set_tree(sample_tree())
    result = run_scripted(
        scripted_shard([Action(action="click", target=ElementQuery(role="button", label="Save"))]),
        driver,
        config,
        worker_id="w1",
        work_dir=work,
    )
    assert result.status == "failed"
    assert [finding.kind for finding in result.findings] == ["crash"]
    assert result.findings[0].severity == "critical"
    assert result.steps[-1].status == "failed"
    assert "crashed after this step" in result.steps[-1].message
