"""verify_fix: build, replay a finding on N devices, and report the verdict."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from swarmqa.driver.fake import FakeDriver
from swarmqa.models import AppTarget, Finding, Shard, UIElement
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.report.findings_json import write_findings_json
from swarmqa.reporter.findings import write_replay
from swarmqa.testing import make_app, sample_config
from swarmqa.verify import build as build_mod
from swarmqa.verify import cli as verify_cli
from swarmqa.verify import run_verify, start_verify, verify_status
from swarmqa.verify.core import plan_devices, verify
from swarmqa.verify.status import INDEX_ENV, STATUS_JSON


@pytest.fixture(autouse=True)
def _index(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(INDEX_ENV, str(tmp_path / "verify-index"))


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
    """The planted-bug app: `Crash Me` crashes and `Refresh` does nothing, unless fixed."""

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


class Factory:
    """Driver factory that records which target and work dir each device got."""

    def __init__(self, *, fixed: bool = False, broken: bool = False):
        self.fixed = fixed
        self.broken = broken
        self.calls: list[tuple[AppTarget, Path]] = []
        self.lock = threading.Lock()

    def __call__(self, target, work_dir):
        with self.lock:
            self.calls.append((target, Path(work_dir)))
        if self.broken:
            target = AppTarget(path=str(Path(work_dir) / "missing.app"))
        return AppFake(target, work_dir, fixed=self.fixed)


@pytest.fixture
def campaign(tmp_path: Path):
    """A campaign from the real agent loop on the planted-bug app."""
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = 1
    config.explorer.engine = "agent"
    config.explorer.max_steps = 40
    result = run_campaign(config, [Shard(id="s1", kind="exploratory", name="crawl")], driver_factory=Factory())
    findings = [finding for worker in result.results for finding in worker.findings]
    crash = next(f for f in findings if f.kind == "crash")
    dead = next(f for f in findings if f.kind == "unresponsive" and "Refresh" in f.title)
    assert (Path(result.report_dir) / "findings.json").is_file()
    return config, Path(result.report_dir), crash, dead


# Verdicts


def test_still_reproduces_on_both_devices_then_passes_once_fixed(campaign):
    config, report_dir, crash, dead = campaign
    factory = Factory()
    result = run_verify(crash.id, config=config, devices=2, driver_factory=factory)
    assert result.state == "failed", result.message
    assert result.devices == 2
    assert result.reproduced_on == ["macos-1", "macos-2"]
    assert "still reproduces on 2 of 2" in result.message
    assert "no app.build_command" in result.message
    # Each device replays in its own directory under the verify run.
    dirs = {work_dir for _target, work_dir in factory.calls}
    assert len(dirs) == 2
    verify_dir = report_dir / "verify" / result.verify_id
    assert all(verify_dir in path.parents for path in dirs)
    assert result.evidence and all(not Path(item).is_absolute() for item in result.evidence)
    assert all((report_dir / item).is_file() for item in result.evidence)
    assert any(item.endswith(".png") for item in result.evidence)
    status = json.loads((verify_dir / STATUS_JSON).read_text())
    assert status["state"] == "failed" and status["phase"] == "done"
    assert [item["state"] for item in status["device_results"]] == ["reproduced", "reproduced"]

    dead_result = run_verify(dead.id, config=config, devices=2, driver_factory=Factory())
    assert dead_result.state == "failed"

    for finding in (crash, dead):
        fixed = run_verify(finding.id, config=config, devices=2, driver_factory=Factory(fixed=True))
        assert fixed.state == "passed", fixed.message
        assert fixed.reproduced_on == []
        assert "did not reproduce on 2 device(s)" in fixed.message


def test_one_device_reproducing_is_enough_to_fail(campaign):
    config, _report_dir, crash, _dead = campaign

    class Flaky(Factory):
        def __call__(self, target, work_dir):
            with self.lock:
                self.calls.append((target, Path(work_dir)))
                first = len(self.calls) == 1
            return AppFake(target, work_dir, fixed=not first)

    result = run_verify(crash.id, config=config, devices=3, driver_factory=Flaky())
    assert result.state == "failed"
    assert len(result.reproduced_on) == 1


def test_an_app_that_cannot_launch_is_an_error(campaign):
    config, _report_dir, crash, _dead = campaign
    result = run_verify(crash.id, config=config, devices=2, driver_factory=Factory(broken=True))
    assert result.state == "error", result.message
    assert result.reproduced_on == []
    assert "could not verify on 2 of 2" in result.message


def test_a_driver_that_cannot_be_created_is_an_error(campaign):
    config, _report_dir, crash, _dead = campaign

    def factory(target, work_dir):
        raise RuntimeError("no simulator")

    result = run_verify(crash.id, config=config, devices=1, driver_factory=factory)
    assert result.state == "error"
    assert "no simulator" in result.message


def test_launch_findings_without_a_replay_get_a_launch_flow(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    campaign_dir = tmp_path / "reports" / "c1"
    launch = Finding(id="f-l", title="App failed to launch", severity="critical", kind="launch",
                     steps=[], fingerprint="l", worker_id="w1", backend="local")
    write_findings_json(campaign_dir, "c1", [launch])
    assert run_verify("f-l", config=config, devices=1, driver_factory=Factory()).state == "passed"
    still = run_verify("f-l", config=config, devices=1, driver_factory=Factory(broken=True))
    assert still.state == "failed" and still.reproduced_on == ["macos-1"]


# Setup errors


def test_unknown_finding_and_campaign(campaign, tmp_path: Path):
    config, report_dir, _crash, _dead = campaign
    missing = run_verify("f-nope", config=config, driver_factory=Factory())
    assert missing.state == "error" and "f-nope not found" in missing.message
    assert not (report_dir / "verify").exists()

    other = run_verify("f-nope", config=config, campaign_id="no-such-campaign", driver_factory=Factory())
    assert other.state == "error" and "campaign not found" in other.message

    config.report_root = str(tmp_path / "empty")
    none = run_verify("f-x", config=config, driver_factory=Factory())
    assert none.state == "error" and "report root not found" in none.message

    assert run_verify("f-x", config=config, devices=0).state == "error"


def test_finding_without_a_replay_is_an_error(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    finding = Finding(id="f-a", title="t", severity="low", kind="assertion", steps=[], fingerprint="a",
                      worker_id="w1", backend="local")
    write_findings_json(tmp_path / "reports" / "c1", "c1", [finding])
    result = run_verify("f-a", config=config, driver_factory=Factory())
    assert result.state == "error" and "no replay flow" in result.message


# Build


def test_build_command_runs_in_the_source_dir_before_replay(campaign, tmp_path: Path):
    config, report_dir, crash, _dead = campaign
    source = tmp_path / "src"
    source.mkdir()
    config.app.source_dir = str(source)
    config.app.build_command = "pwd > built.txt && echo building"
    result = run_verify(crash.id, config=config, devices=1, driver_factory=Factory(fixed=True))
    assert result.state == "passed", result.message
    assert (source / "built.txt").read_text().strip() == str(source.resolve())
    log = report_dir / "verify" / result.verify_id / "build.log"
    assert "building" in log.read_text()
    assert f"verify/{result.verify_id}/build.log" in result.evidence


def test_build_failure_stops_before_replay(campaign):
    config, _report_dir, crash, _dead = campaign
    config.app.build_command = "echo compiling; echo 'error: missing semicolon' >&2; exit 3"
    factory = Factory()
    result = run_verify(crash.id, config=config, devices=2, driver_factory=factory)
    assert result.state == "error"
    assert "build failed (exit 3)" in result.message
    assert "error: missing semicolon" in result.message
    assert factory.calls == []


def test_build_timeout(campaign, monkeypatch):
    config, _report_dir, crash, _dead = campaign
    config.app.build_command = "echo started; sleep 30"
    monkeypatch.setattr(build_mod, "BUILD_TIMEOUT_S", 0.5)
    started = time.monotonic()
    result = run_verify(crash.id, config=config, devices=1, driver_factory=Factory())
    assert time.monotonic() - started < 10
    assert result.state == "error"
    assert "build timed out after 0.5s" in result.message and "started" in result.message


def test_no_build_skips_the_command(campaign, tmp_path: Path):
    config, _report_dir, crash, _dead = campaign
    config.app.build_command = f"touch {tmp_path / 'ran'}"
    result = run_verify(crash.id, config=config, devices=1, build=False, driver_factory=Factory(fixed=True))
    assert result.state == "passed"
    assert not (tmp_path / "ran").exists()
    assert "build skipped" in result.message


def test_builder_is_swappable(campaign):
    config, _report_dir, crash, _dead = campaign
    seen = []

    def builder(cfg, log_path):
        seen.append(log_path)
        return build_mod.BuildOutcome(ok=False, message="no toolchain")

    run = verify(crash.id, config=config, devices=1, driver_factory=Factory(), builder=builder)
    assert run.result.state == "error" and run.result.message == "no toolchain"
    assert seen and seen[0].name == "build.log"


# Devices


def test_ios_simulators_spread_across_devices(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.app.platform = "ios"
    config.app.simulators = ["iPhone 15", "iPhone SE", "iPhone 15"]
    slots = plan_devices(config, 2)
    assert [slot.name for slot in slots] == ["iPhone 15", "iPhone SE"]
    assert [slot.target.simulators for slot in slots] == [["iPhone 15"], ["iPhone SE"]]
    assert slots[0].target.simulator == "iPhone 15"
    assert config.app.simulators == ["iPhone 15", "iPhone SE", "iPhone 15"]

    wrapped = plan_devices(config, 3)
    assert [slot.name for slot in wrapped] == ["iPhone 15#1", "iPhone SE#1", "iPhone 15#2"]
    assert wrapped[0].lock_key == wrapped[2].lock_key != wrapped[1].lock_key


def test_host_devices_run_one_at_a_time_with_a_real_driver(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    assert {slot.lock_key for slot in plan_devices(config, 3)} == {"host"}
    assert len({slot.lock_key for slot in plan_devices(config, 3, injected=True)}) == 3
    config.driver.kind = "fake"
    assert len({slot.lock_key for slot in plan_devices(config, 3)}) == 3


def test_ios_verify_hands_each_device_its_simulator(campaign):
    config, _report_dir, crash, _dead = campaign
    config.app.platform = "ios"
    config.app.simulators = ["sim-a", "sim-b"]
    factory = Factory()
    result = run_verify(crash.id, config=config, devices=2, driver_factory=factory)
    assert sorted(result.reproduced_on) == ["sim-a", "sim-b"]
    assert sorted(target.simulator for target, _dir in factory.calls) == ["sim-a", "sim-b"]


# Detached runs


def _tiny_config(tmp_path: Path, steps: list[dict]) -> Path:
    app = make_app(tmp_path)
    campaign_dir = tmp_path / "reports" / "20260101T000000-c1"
    finding = Finding(id="f-1", title="Missing control: Gone", severity="medium", kind="missing_control",
                      steps=[], fingerprint="zz", worker_id="w1", backend="local")
    replay = write_replay("f-1", campaign_dir, steps)
    finding.replay_json = finding.repro = replay.relative_to(campaign_dir).as_posix()
    write_findings_json(campaign_dir, "c1", [finding])
    path = tmp_path / "aqa.config.toml"
    path.write_text(
        f'[campaign]\nreport_root = "{tmp_path / "reports"}"\n'
        f'[app]\npath = "{app.path}"\n'
        '[driver]\nkind = "fake"\n'
        '[video]\nmode = "on_failure"\n',
        encoding="utf-8",
    )
    return path


def _wait(verify_id: str, timeout_s: float = 60.0):
    deadline = time.monotonic() + timeout_s
    while True:
        status = verify_status(verify_id)
        if status.state != "running" or time.monotonic() > deadline:
            return status
        time.sleep(0.1)


def test_start_verify_runs_detached_and_passes(tmp_path: Path):
    config_path = _tiny_config(tmp_path, [{"action": "launch"}, {"action": "key", "keys": ["escape"]}])
    started = start_verify("f-1", config_path=config_path, devices=2)
    assert started.state == "running" and started.verify_id.startswith("v-")
    verify_dir = tmp_path / "reports" / "20260101T000000-c1" / "verify" / started.verify_id
    assert (verify_dir / STATUS_JSON).is_file() and (verify_dir / "runner.pid").is_file()
    done = _wait(started.verify_id)
    assert done.state == "passed", (done.message, (verify_dir / "runner.log").read_text())
    assert done.devices == 2 and done.finding_id == "f-1"
    # The report root finds it too, without the index.
    assert verify_status(started.verify_id, report_root=tmp_path / "reports").state == "passed"


def test_start_verify_reports_a_reproducing_finding(tmp_path: Path):
    config_path = _tiny_config(tmp_path, [{"action": "launch"}, {"action": "click", "target": {"label": "Gone"}}])
    started = start_verify("f-1", config_path=config_path, devices=1, build=False)
    done = _wait(started.verify_id)
    assert done.state == "failed", done.message
    assert done.reproduced_on == ["macos-1"]


def test_start_verify_errors_come_back_at_once(tmp_path: Path):
    config_path = _tiny_config(tmp_path, [{"action": "launch"}])
    assert start_verify("f-nope", config_path=config_path).state == "error"
    missing = start_verify("f-1", config_path=tmp_path / "none.toml")
    assert missing.state == "error" and "not found" in missing.message
    assert verify_status("v-unknown").state == "error"


def test_verify_status_reports_a_dead_runner(tmp_path: Path):
    verify_dir = tmp_path / "reports" / "c1" / "verify" / "v-dead"
    verify_dir.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    (verify_dir / STATUS_JSON).write_text(json.dumps(
        {"verify_id": "v-dead", "finding_id": "f-1", "state": "running", "devices": 2}
    ))
    (verify_dir / "runner.pid").write_text(f"{process.pid}\n")
    (verify_dir / "runner.log").write_text("Traceback: boom\n")
    status = verify_status("v-dead", report_root=tmp_path / "reports")
    assert status.state == "error"
    assert "exited without finishing" in status.message and "boom" in status.message

    # A live pid keeps it running.
    (verify_dir / "runner.pid").write_text(f"{__import__('os').getpid()}\n")
    assert verify_status("v-dead", report_root=tmp_path / "reports").state == "running"


# CLI


def test_cli_exit_codes(campaign, tmp_path: Path, capsys):
    config, report_dir, crash, _dead = campaign
    config_path = tmp_path / "aqa.config.toml"
    config_path.write_text(
        f'[campaign]\nreport_root = "{report_dir.parent}"\n[app]\npath = "{config.app.path}"\n', encoding="utf-8"
    )
    base = [crash.id, "--config", str(config_path)]

    assert verify_cli.main(base, driver_factory=Factory()) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"{crash.id} on macos-1: reproduced (the final step crashed the app)",
        f"{crash.id} on macos-2: reproduced (the final step crashed the app)",
    ]

    assert verify_cli.main(base + ["--devices", "3", "--no-build"], driver_factory=Factory(fixed=True)) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3

    assert verify_cli.main(base, driver_factory=Factory(broken=True)) == 2
    assert "could not replay" in capsys.readouterr().out

    assert verify_cli.main(["f-nope", "--config", str(config_path)], driver_factory=Factory()) == 2
    assert "f-nope not found" in capsys.readouterr().err

    bad = tmp_path / "bad.toml"
    bad.write_text("workers = [", encoding="utf-8")
    assert verify_cli.main([crash.id, "--config", str(bad)]) == 2


def test_runner_records_a_setup_error_instead_of_leaving_running(tmp_path):
    from swarmqa.mcp.tools import VerifyResult
    from swarmqa.verify import _runner
    from swarmqa.verify import status as status_mod

    status_dir = tmp_path / "verify" / "v1"
    status_mod.write_status(status_dir, VerifyResult("v1", "f-x", "running", devices=1))
    config = tmp_path / "aqa.config.toml"
    config.write_text(f'report_root = "{tmp_path / "reports"}"\n', encoding="utf-8")
    code = _runner.main(["f-x", "--config", str(config), "--campaign", "nope", "--devices", "1",
                         "--verify-id", "v1", "--status-dir", str(status_dir)])
    assert code == 2
    document = status_mod.read_document(status_dir / status_mod.STATUS_JSON)
    assert document["state"] == "error" and document["message"]
