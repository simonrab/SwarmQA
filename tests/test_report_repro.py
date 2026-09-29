"""Repro files and replaying findings against the fake driver."""

from __future__ import annotations

import json
from pathlib import Path

from swarmqa.checks import default_checks
from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.models import Finding, Shard, UIElement
from swarmqa.report.findings_json import write_findings_json
from swarmqa.report.repro import ensure_repro, replay_finding, reproduction_rule
from swarmqa.reporter.findings import write_replay
from swarmqa.testing import make_app, sample_config


def _window(*children: UIElement) -> list[UIElement]:
    return [UIElement(role="window", label="Main", frame=(0, 0, 390, 844), children=list(children))]


def _home() -> list[UIElement]:
    return _window(
        UIElement(role="button", label="Open Settings", identifier="home.settings", frame=(20, 100, 200, 44)),
        UIElement(role="button", label="Refresh", identifier="home.refresh", frame=(20, 200, 200, 44)),
        UIElement(role="button", label="Crash Me", identifier="home.crash", frame=(20, 300, 200, 44)),
    )


def _settings() -> list[UIElement]:
    return _window(UIElement(role="button", label="Back", identifier="settings.back", frame=(0, 40, 60, 44)))


class AppFake(FakeDriver):
    """The planted-bug app; every launch starts on the home screen."""

    def __init__(self, *args, fixed: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.transitions = {"Open Settings": _settings(), "Back": _home()}
        if fixed:
            self.transitions["Refresh"] = _window(UIElement(role="text", label="Refreshed"))
        else:
            self.crash_labels = {"Crash Me"}

    def launch(self) -> None:
        super().launch()
        self.tree = _home()


def _explore(tmp_path: Path) -> tuple[Path, list[Finding]]:
    campaign = tmp_path / "campaign"
    result = run_agent_loop(
        Shard(id="s1", kind="exploratory", name="crawl"),
        AppFake(make_app(tmp_path), tmp_path / "work"),
        sample_config(make_app(tmp_path)),
        checks=default_checks(),
        settings=AgentLoopSettings(mode="crawl", max_steps=40),
        work_dir=campaign / "workers" / "w1",
        worker_id="w1",
    )
    return campaign, result.findings


def _pick(findings: list[Finding], kind: str, word: str) -> Finding:
    return next(f for f in findings if f.kind == kind and word in f.title)


def test_ensure_repro_reuses_the_replay_and_writes_the_command(tmp_path: Path):
    campaign, findings = _explore(tmp_path)
    dead = _pick(findings, "unresponsive", "Refresh")
    replay = campaign / dead.replay_json
    before = replay.read_text()
    path = ensure_repro(campaign, dead)
    assert path == replay and replay.read_text() == before
    assert dead.repro == dead.replay_json == f"findings/{dead.id}.replay.json"
    text = (campaign / "findings" / f"{dead.id}.repro.md").read_text()
    assert f"aqa replay {replay}" in text
    assert reproduction_rule("unresponsive") in text
    assert '"action": "launch"' in text


def test_ensure_repro_for_launch_and_missing_replays(tmp_path: Path):
    launch = Finding(id="f-l", title="App failed to launch", severity="critical", kind="launch",
                     steps=[], fingerprint="l", worker_id="w1", backend="local")
    path = ensure_repro(tmp_path, launch)
    assert json.loads(path.read_text())["steps"] == [{"action": "launch"}]
    assert launch.repro == "findings/f-l.replay.json"
    other = Finding(id="f-x", title="t", severity="low", kind="assertion", steps=[], fingerprint="x",
                    worker_id="w1", backend="local")
    assert ensure_repro(tmp_path, other) is None and other.repro is None


def test_replay_reproduces_a_planted_crash(tmp_path: Path):
    campaign, findings = _explore(tmp_path)
    crash = _pick(findings, "crash", "Crash")
    write_findings_json(campaign, "c1", findings)
    replay = campaign / crash.replay_json
    config = sample_config(make_app(tmp_path))

    outcome = replay_finding(replay, config, driver=AppFake(make_app(tmp_path), tmp_path / "r1"), work_dir=tmp_path / "r1")
    assert outcome.finding_id == crash.id and outcome.kind == "crash"
    assert outcome.reproduced, outcome.reason
    assert outcome.crashed

    fixed = replay_finding(
        replay, config, driver=AppFake(make_app(tmp_path), tmp_path / "r2", fixed=True), work_dir=tmp_path / "r2"
    )
    assert not fixed.reproduced and not fixed.crashed


def test_replay_reproduces_a_dead_tap_by_fingerprint(tmp_path: Path):
    campaign, findings = _explore(tmp_path)
    dead = _pick(findings, "unresponsive", "Refresh")
    config = sample_config(make_app(tmp_path))
    replay = campaign / dead.replay_json

    outcome = replay_finding(
        replay, config, driver=AppFake(make_app(tmp_path), tmp_path / "r1"), work_dir=tmp_path / "r1", finding=dead
    )
    assert outcome.reproduced, outcome.reason
    assert outcome.reason == "a check reported the same fingerprint"
    assert dead.fingerprint in outcome.observed_fingerprints
    assert not outcome.final_step_failed

    fixed = replay_finding(
        replay,
        config,
        driver=AppFake(make_app(tmp_path), tmp_path / "r2", fixed=True),
        work_dir=tmp_path / "r2",
        finding=dead,
    )
    assert not fixed.reproduced
    assert fixed.reason == "the flow ran and the finding was not seen"


def test_replay_of_a_hand_written_flow_uses_the_final_step(tmp_path: Path):
    campaign = tmp_path / "campaign"
    finding = Finding(id="f-9", title="Missing control: Gone", severity="medium", kind="missing_control",
                      steps=[], fingerprint="zz", worker_id="w1", backend="local")
    replay = write_replay("f-9", campaign, [
        {"action": "launch"},
        {"action": "tap_point", "point": [120.0, 122.0]},
        {"action": "click", "target": {"label": "Back"}},
        {"action": "click", "target": {"label": "Gone"}},
    ])
    config = sample_config(make_app(tmp_path))
    driver = AppFake(make_app(tmp_path), tmp_path / "r")
    outcome = replay_finding(replay, config, driver=driver, work_dir=tmp_path / "r", finding=finding)
    assert outcome.reproduced and outcome.final_step_failed
    assert outcome.reason == "the final step failed"
    assert driver.actions_log.count("launch") == 1  # the recorded launch step is not repeated
    assert "tap:120,122" in driver.actions_log

    # Without finding metadata (no findings.json), any failed step counts.
    bare = replay_finding(replay, config, driver=AppFake(make_app(tmp_path), tmp_path / "b"), work_dir=tmp_path / "b")
    assert bare.reproduced and "no finding metadata" in bare.reason


def test_replay_of_a_launch_failure(tmp_path: Path):
    finding = Finding(id="f-l", title="App failed to launch", severity="critical", kind="launch",
                      steps=[], fingerprint="l", worker_id="w1", backend="local")
    replay = write_replay("f-l", tmp_path / "campaign", [{"action": "launch"}])
    config = sample_config(make_app(tmp_path))
    broken = FakeDriver(make_app(tmp_path).__class__(path=str(tmp_path / "missing.app")), tmp_path / "d")
    outcome = replay_finding(replay, config, driver=broken, work_dir=tmp_path / "d", finding=finding)
    assert outcome.reproduced and outcome.reason == "the app failed to launch"
    ok = replay_finding(replay, config, driver=AppFake(make_app(tmp_path), tmp_path / "o"), work_dir=tmp_path / "o",
                        finding=finding)
    assert not ok.reproduced


def test_replay_creates_the_configured_driver(tmp_path: Path):
    replay = write_replay("f-1", tmp_path / "campaign", [{"action": "launch"}, {"action": "key", "keys": ["escape"]}])
    config = sample_config(make_app(tmp_path))
    config.driver.kind = "fake"
    outcome = replay_finding(replay, config, work_dir=tmp_path / "w")
    assert not outcome.reproduced
    assert outcome.result is not None and outcome.result.status == "passed"
