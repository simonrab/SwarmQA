"""`aqa try`: resolve the app, build the config, run, report. No Xcode or simulator needed."""

from __future__ import annotations

import io
import json
import plistlib
import subprocess
from pathlib import Path

import pytest

from swarmqa.build.builder import BuildResult
from swarmqa.build.errors import BuildFailure
from swarmqa.config import load_config
from swarmqa.driver.fake import FakeDriver
from swarmqa.models import BuildArtifact, CampaignResult, Finding, UIElement, WorkerResult
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.quickstart.cli import TryEnv, main, make_queue
from swarmqa.quickstart.resolve import AppInfo, TryError, pick_simulator, read_app, supported_platforms
from swarmqa.quickstart.save import render_config, save_starter
from swarmqa.testing import make_app

DEFAULT_UDID = "11111111-2222-3333-4444-555555555555"
OTHER_UDID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


def _devices(*entries: tuple[str, str, str]) -> str:
    runtime = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
    return json.dumps({"devices": {runtime: [
        {"name": name, "udid": udid, "state": state, "isAvailable": True} for name, udid, state in entries
    ]}})


SIMS = _devices(("iPhone 17", DEFAULT_UDID, "Shutdown"), ("iPad Air", OTHER_UDID, "Booted"))


class FakeRunner:
    def __init__(self, replies: dict[tuple[str, ...], tuple[int, str]] | None = None):
        self.replies = {("xcrun", "simctl", "list"): (0, SIMS), **(replies or {})}
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        for prefix, (code, out) in self.replies.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(args, code, out, "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected command")


class Harness:
    def __init__(self, tmp_path: Path, *, results=None, runner=None, runner_state="ready", html=None,
                 tty=False, host="darwin", environ=None, builder=None, run=None):
        self.tmp_path = tmp_path
        self.calls: list = []
        self.opened: list[Path] = []
        self.out, self.err = io.StringIO(), io.StringIO()
        self.results = results or []
        self.runner = runner or FakeRunner()
        self.env = TryEnv(
            run_campaign=run or self._run,
            runner=self.runner,
            make_builder=builder or (lambda settings: pytest.fail("unexpected build")),
            runner_state=lambda platform: runner_state,
            write_html=lambda root: html,
            opener=self.opened.append,
            is_tty=lambda: tty,
            host=host,
            environ=environ or {},
            has_module=lambda name: True,
            cwd=tmp_path,
            out=self.out,
            err=self.err,
        )

    def _run(self, config, queue):
        self.calls.append((config, queue))
        report = self.tmp_path / "reports" / "c1"
        report.mkdir(parents=True, exist_ok=True)
        return CampaignResult("c1", str(report), results=self.results)

    def main(self, *argv: str) -> int:
        return main(list(argv), env=self.env)


def _ios_app(directory: Path, platforms=("iPhoneSimulator",)) -> Path:
    app = directory / "Demo.app"
    app.mkdir(parents=True)
    with (app / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "dev.swarmqa.PlantedBugs",
                       "CFBundleSupportedPlatforms": list(platforms)}, handle)
    return app


def _finding(fid: str, severity: str) -> Finding:
    return Finding(id=fid, title=fid, severity=severity, kind="crash", steps=[], fingerprint=fid,
                   worker_id="w1", backend="local")


# Resolving the app


def test_read_app_finds_platform_and_bundle_id(tmp_path: Path):
    ios = read_app(_ios_app(tmp_path / "ios"))
    assert (ios.platform, ios.bundle_id, ios.name) == ("ios", "dev.swarmqa.PlantedBugs", "Demo")
    mac = read_app(Path(make_app(tmp_path / "mac").path))
    assert (mac.platform, mac.bundle_id) == ("macos", "dev.swarmqa.sample")


def test_read_app_rejects_device_builds_and_folders(tmp_path: Path):
    with pytest.raises(TryError, match="not for the Simulator"):
        read_app(_ios_app(tmp_path / "dev", platforms=("iPhoneOS",)))
    with pytest.raises(TryError, match="not an app bundle"):
        read_app(tmp_path)


def test_pick_simulator_prefers_default_then_any_iphone():
    assert pick_simulator(FakeRunner(), None) == (DEFAULT_UDID, "iPhone 17")
    runner = FakeRunner({("xcrun", "simctl", "list"): (0, _devices(("iPhone 15", OTHER_UDID, "Shutdown")))})
    assert pick_simulator(runner, None) == (OTHER_UDID, "iPhone 15")
    assert pick_simulator(FakeRunner(), OTHER_UDID) == (OTHER_UDID, OTHER_UDID)
    with pytest.raises(TryError, match="No iPhone Simulator"):
        pick_simulator(FakeRunner({("xcrun", "simctl", "list"): (0, _devices())}), None)
    with pytest.raises(TryError, match="--simulator"):
        pick_simulator(FakeRunner(), "iPhone 99")


def test_supported_platforms_reads_app_targets_only():
    text = json.dumps([
        {"buildSettings": {"WRAPPER_EXTENSION": "app", "SUPPORTED_PLATFORMS": "macosx"}},
        {"buildSettings": {"WRAPPER_EXTENSION": "framework", "SUPPORTED_PLATFORMS": "iphoneos"}},
    ])
    assert supported_platforms("noise\n" + text) == {"macosx"}


# Running a built app


def test_try_ios_app_builds_config_and_runs(tmp_path: Path):
    app = _ios_app(tmp_path)
    harness = Harness(tmp_path)
    code = harness.main(str(app), "--minutes", "2", "--goal", "Add a plant", "--goal", "Delete it")
    assert code == 0, harness.err.getvalue()
    config, queue = harness.calls[0]
    assert (config.app.platform, config.app.bundle_id, config.app.path) == ("ios", "dev.swarmqa.PlantedBugs", str(app))
    assert config.app.simulator == DEFAULT_UDID
    assert (config.driver.kind, config.explorer.engine, config.workers, config.video.mode) == (
        "runner", "agent", 1, "on_failure")
    assert config.explorer.max_time_s == 40
    assert config.budgets.max_wall_time_s == 120 + 3 * 180  # exploring plus start-up for 3 shards
    assert config.llm.enabled is False
    assert [(shard.kind, shard.goal) for shard in queue] == [
        ("exploratory", "Add a plant"), ("exploratory", "Delete it"), ("exploratory", "")]
    out = harness.out.getvalue()
    assert "iPhone 17" in out and "Model: none (free)" in out
    assert "No problems found." in out and "summary.md" in out and "aqa try --save" in out
    assert harness.opened == []


def test_try_summarises_findings_and_opens_html(tmp_path: Path):
    app = make_app(tmp_path)
    results = [WorkerResult("w1", "s-crawl", "failed", findings=[
        _finding("a", "critical"), _finding("b", "high"), _finding("c", "high"), _finding("a", "critical")])]
    html = tmp_path / "reports" / "c1" / "report.html"
    harness = Harness(tmp_path, results=results, html=html, tty=True)
    assert harness.main(app.path) == 0
    out = harness.out.getvalue()
    assert "Found 3 problem(s): 1 critical, 2 high." in out
    assert f"Report: {html}" in out
    assert harness.opened == [html]
    assert harness.calls[0][0].app.simulator is None

    quiet = Harness(tmp_path, results=results, html=html, tty=True)
    assert quiet.main(app.path, "--no-open") == 0
    assert quiet.opened == []


def test_try_exits_2_when_every_shard_errors(tmp_path: Path):
    app = make_app(tmp_path)
    results = [WorkerResult("w1", "s-crawl", "error", error="runner did not start")]
    harness = Harness(tmp_path, results=results)
    assert harness.main(app.path) == 2
    assert "runner did not start" in harness.err.getvalue()


def test_model_flag_uses_the_available_key(tmp_path: Path):
    app = make_app(tmp_path)
    harness = Harness(tmp_path, environ={"OPENAI_API_KEY": "x"})
    assert harness.main(app.path, "--model") == 0
    config = harness.calls[0][0]
    assert config.llm.enabled is True and config.llm.settings["provider"] == "openai"

    keyless = Harness(tmp_path)
    assert keyless.main(app.path, "--model") == 0
    assert keyless.calls[0][0].llm.enabled is False
    assert "needs ANTHROPIC_API_KEY or OPENAI_API_KEY" in keyless.out.getvalue()


# Problems that stop the run


@pytest.mark.parametrize(
    ("kwargs", "argv", "message"),
    [
        ({"host": "linux"}, ["App.app"], "on a Mac"),
        ({}, [], "no app or Xcode project here"),
        ({}, ["missing.app"], "does not exist"),
        ({"runner_state": "missing"}, None, "agents/swarm-runner/build.sh macos"),
        ({}, ["--minutes", "0"], "--minutes"),
    ],
)
def test_try_stops_with_a_fix(tmp_path: Path, kwargs, argv, message):
    harness = Harness(tmp_path, **kwargs)
    if argv is None:
        argv = [make_app(tmp_path).path]
    assert harness.main(*argv) == 2
    assert message in harness.err.getvalue()
    assert "Traceback" not in harness.err.getvalue()
    assert harness.calls == []


def test_try_rejects_a_platform_mismatch_and_bad_flags(tmp_path: Path):
    app = make_app(tmp_path)
    harness = Harness(tmp_path)
    assert harness.main(app.path, "--platform", "ios") == 2
    assert "is a macos app" in harness.err.getvalue()
    assert harness.main("--platform", "android") == 2


def test_runner_build_note_when_build_script_exists(tmp_path: Path):
    harness = Harness(tmp_path, runner_state="build")
    assert harness.main(make_app(tmp_path).path) == 0
    assert "not built yet" in harness.out.getvalue()


# Building a project


class FakeBuilder:
    def __init__(self, artifact: BuildArtifact | None = None, failure: BuildFailure | None = None):
        self.artifact, self.failure = artifact, failure
        self.settings = None
        self.calls: list = []

    def __call__(self, settings):
        self.settings = settings
        return self

    def build_result(self, sha, platform, *, repo=None, force=False):
        self.calls.append((sha, platform, repo))
        return BuildResult(platform, sha, artifact=self.artifact, failure=self.failure)


def _project(tmp_path: Path) -> tuple[Path, FakeRunner]:
    repo = tmp_path / "repo"
    (repo / "Demo.xcodeproj").mkdir(parents=True)
    settings = json.dumps([{"buildSettings": {"WRAPPER_EXTENSION": "app", "SUPPORTED_PLATFORMS": "macosx"}}])
    runner = FakeRunner({
        ("git", "rev-parse", "--show-toplevel"): (0, str(repo.resolve()) + "\n"),
        ("git", "rev-parse", "HEAD"): (0, "abcdef0123456789\n"),
        ("git", "status"): (0, " M Demo/View.swift\n"),
        ("xcodebuild", "-list"): (0, json.dumps({"project": {"name": "Demo", "schemes": ["Demo", "DemoTests"]}})),
        ("xcodebuild", "-showBuildSettings"): (0, settings),
    })
    return repo, runner


def test_try_project_folder_builds_head(tmp_path: Path):
    repo, runner = _project(tmp_path)
    app = make_app(tmp_path / "built")
    builder = FakeBuilder(BuildArtifact("macos", app.path, "dev.swarmqa.sample", "abcdef0123456789"))
    harness = Harness(tmp_path, runner=runner, builder=builder)
    harness.env.cwd = repo
    assert harness.main() == 0, harness.err.getvalue()
    assert builder.calls == [("abcdef0123456789", "macos", str(repo.resolve()))]
    assert (builder.settings.scheme, builder.settings.project) == ("Demo", "Demo.xcodeproj")
    config = harness.calls[0][0]
    assert config.app.source_dir == str(repo.resolve()) and config.app.path == app.path
    assert "uncommitted changes" in harness.err.getvalue()
    assert "Building Demo for macos" in harness.out.getvalue()


def test_try_project_platform_flag_and_build_failure(tmp_path: Path):
    repo, runner = _project(tmp_path)
    failure = BuildFailure("repo", "abc", "ios", "build", "xcodebuild failed (exit 65)", "/tmp/build.log",
                           "View.swift:3: error: cannot find 'x' in scope")
    builder = FakeBuilder(failure=failure)
    harness = Harness(tmp_path, runner=runner, builder=builder)
    assert harness.main(str(repo / "Demo.xcodeproj"), "--platform", "ios", "--scheme", "Demo") == 2
    assert builder.calls[0][1] == "ios"
    err = harness.err.getvalue()
    assert "error: cannot find 'x' in scope" in err and "/tmp/build.log" in err
    assert not any(call[:2] == ["xcodebuild", "-list"] for call in runner.calls)


def test_try_project_outside_git_says_how_to_fix(tmp_path: Path):
    repo, _ = _project(tmp_path)
    harness = Harness(tmp_path, runner=FakeRunner())
    assert harness.main(str(repo)) == 2
    assert "not in a git repository" in harness.err.getvalue()


# --save


def test_save_writes_starter_files_once(tmp_path: Path):
    app = AppInfo("ios", "dev.swarmqa.PlantedBugs", "/apps/PlantedBugs.app", "/src/PlantedBugs", "PlantedBugs")
    assert save_starter(tmp_path, app, "iPhone 17") == ["wrote aqa.config.toml", "wrote intents/example-goal.md"]
    config = load_config(tmp_path / "aqa.config.toml")
    assert (config.app.platform, config.app.bundle_id, config.app.path) == (
        "ios", "dev.swarmqa.PlantedBugs", "/apps/PlantedBugs.app")
    assert (config.app.source_dir, config.app.simulator) == ("/src/PlantedBugs", "iPhone 17")
    assert (config.driver.kind, config.explorer.engine) == ("runner", "agent")
    (tmp_path / "aqa.config.toml").write_text("# mine\n", encoding="utf-8")
    assert save_starter(tmp_path, app)[0] == "kept aqa.config.toml (it already exists)"
    assert (tmp_path / "aqa.config.toml").read_text(encoding="utf-8") == "# mine\n"


def test_try_save_flag_prints_what_it_wrote(tmp_path: Path):
    harness = Harness(tmp_path)
    assert harness.main(make_app(tmp_path).path, "--save") == 0
    assert "save: wrote aqa.config.toml" in harness.out.getvalue()
    assert 'platform = "macos"' in render_config(read_app(tmp_path / "Sample.app"))
    assert (tmp_path / "intents" / "example-goal.md").is_file()


def test_try_config_flag_keeps_settings(tmp_path: Path):
    path = tmp_path / "base.toml"
    path.write_text('[campaign]\nworkers = 3\n[video]\nmode = "always"\n', encoding="utf-8")
    harness = Harness(tmp_path)
    assert harness.main(make_app(tmp_path).path, "--config", str(path), "--report-root", "out") == 0
    config = harness.calls[0][0]
    assert (config.workers, config.video.mode, config.report_root, config.driver.kind) == (3, "always", "out", "runner")
    bad = tmp_path / "bad.toml"
    bad.write_text("[campaign]\nworkers = 0\n", encoding="utf-8")
    assert harness.main(make_app(tmp_path).path, "--config", str(bad)) == 2


def test_make_queue_skips_blank_goals():
    assert [shard.id for shard in make_queue(["  ", "Sign in"])] == ["s-goal-1", "s-crawl"]


# End to end with the real orchestrator and the fake driver


def test_try_end_to_end_with_fake_driver(tmp_path: Path):
    def factory(target, work_dir):
        driver = FakeDriver(target, work_dir)
        driver.set_tree([UIElement(role="window", label="Main", frame=(0, 0, 800, 600), children=[
            UIElement(role="button", label="Refresh", identifier="home.refresh", frame=(20, 100, 200, 44)),
        ])])
        return driver

    harness = Harness(tmp_path, run=lambda config, queue: run_campaign(config, queue, driver_factory=factory))
    code = harness.main(make_app(tmp_path).path, "--minutes", "0.2", "--report-root", str(tmp_path / "reports"))
    assert code == 0, harness.err.getvalue()
    summaries = list((tmp_path / "reports").glob("*/summary.md"))
    assert len(summaries) == 1
    assert str(summaries[0].resolve()) in harness.out.getvalue()
