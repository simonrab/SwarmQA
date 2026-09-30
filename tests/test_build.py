"""WP-C4: per-SHA builder. Fake git and xcodebuild runners; runs on Linux."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from swarmqa.build import (
    BuildConfigError,
    BuildFailed,
    BuildSettings,
    RepoCache,
    ShaBuilder,
    build_failure_finding,
    repo_name_for,
)
from swarmqa.build import xcode
from swarmqa.build.errors import BuildFailure, log_excerpt
from swarmqa.build.verify_adapter import verify_builder
from swarmqa.models import CampaignConfig, Finding, category_for_kind
from swarmqa.verify.build import BuildOutcome

SHA_A = "a" * 40
SHA_B = "b" * 40
SOURCE = "git@github.com:acme/PlantedBugs.git"

# Recorded from `xcodebuild -list -json` in fixtures/PlantedBugs (Xcode 26.5).
LIST_FIXTURE = """{
  "project" : {
    "configurations" : [
      "Debug",
      "Release"
    ],
    "name" : "PlantedBugs",
    "schemes" : [
      "PlantedBugs"
    ],
    "targets" : [
      "PlantedBugs"
    ]
  }
}
"""

LIST_WORKSPACE = """Command line invocation:
    /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild -list -json
{
  "workspace" : {
    "name" : "Shop",
    "schemes" : [
      "Pods-Shop",
      "ShopKit",
      "ShopUITests",
      "ShopApp"
    ]
  }
}
"""


def make_app(path: Path, bundle_id: str, platform: str, *, binary: bool = False) -> Path:
    plist = path / "Contents" / "Info.plist" if platform == "macos" else path / "Info.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    data = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": path.stem}
    with plist.open("wb") as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML)
    return path


@dataclass
class FakeRunner:
    """Pretends to be git and xcodebuild. `commits` maps refs to SHAs that
    the mirror has; `remote_only` ones appear after a fetch."""

    commits: dict[str, str] = field(default_factory=lambda: {"HEAD": SHA_A, "main": SHA_A})
    remote_only: dict[str, str] = field(default_factory=dict)
    list_output: str = LIST_FIXTURE
    bundle_id: str = "dev.swarmqa.PlantedBugs"
    fail_build: str | None = None
    shell: object = None
    delay: float = 0.0
    calls: list[list[str]] = field(default_factory=list)
    kwargs: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_builds: int = 0
    max_active_builds: int = 0

    def __call__(self, args, **kwargs):
        with self.lock:
            self.calls.append(list(args))
            self.kwargs.append(kwargs)
        tool = args[0]
        if tool == "git":
            return self._git(args[1:], kwargs)
        if tool == "xcodebuild":
            return self._xcodebuild(args[1:], kwargs)
        if tool == "/bin/sh":
            return self.shell(args[2], kwargs)
        if tool in ("codesign", "xattr"):
            return _done(0)
        raise FileNotFoundError(tool)  # ditto and anything else: "not installed"

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if _matches(call, prefix))

    def _git(self, args, kwargs):
        mirror = None
        if args and args[0].startswith("--git-dir="):
            mirror = Path(args[0].split("=", 1)[1])
            args = args[1:]
        verb = args[0]
        if verb == "clone":
            destination = Path(args[-1])
            destination.mkdir(parents=True)
            (destination / "HEAD").write_text("ref: refs/heads/main\n")
            return _done(0)
        if verb == "rev-parse" and args[1] == "HEAD":
            gitfile = Path(kwargs["cwd"]) / ".git"
            return _done(0, gitfile.read_text().split("/")[-1].strip() + "\n")
        if verb == "rev-parse":
            ref = args[-1].removesuffix("^{commit}")
            for name, sha in self.commits.items():
                if ref in (name, sha) or (len(ref) >= 7 and sha.startswith(ref)):
                    return _done(0, sha + "\n")
            return _done(1)
        if verb == "fetch":
            self.commits.update(self.remote_only)
            return _done(0)
        if verb == "worktree" and args[1] == "add":
            path, sha = Path(args[-2]), args[-1]
            path.mkdir(parents=True, exist_ok=True)
            (path / ".git").write_text(f"gitdir: {mirror}/worktrees/{sha}\n")
            project = path / "fixtures" / "PlantedBugs" / "PlantedBugs.xcodeproj"
            project.mkdir(parents=True)
            return _done(0)
        if verb == "worktree" and args[1] == "remove":
            import shutil

            shutil.rmtree(args[-1], ignore_errors=True)
            return _done(0)
        return _done(0)

    def _xcodebuild(self, args, kwargs):
        if "-list" in args:
            return _done(0, self.list_output)
        if "-showBuildSettings" in args:
            return _done(0, "[]")
        with self.lock:
            self.active_builds += 1
            self.max_active_builds = max(self.max_active_builds, self.active_builds)
        try:
            if self.delay:
                time.sleep(self.delay)
            scheme = args[args.index("-scheme") + 1]
            if self.fail_build and scheme == self.fail_build:
                Path(kwargs["log_path"]).open("a").write(
                    "Compiling...\n/x/Foo.swift:3:5: error: cannot find 'bar' in scope\n"
                    "xcodebuild: error: The project does not contain a scheme named 'Bogus'.\n** BUILD FAILED **\n")
                return _done(65)
            derived = Path(args[args.index("-derivedDataPath") + 1])
            config = args[args.index("-configuration") + 1]
            platform = "ios" if "iOS Simulator" in args[args.index("-destination") + 1] else "macos"
            make_app(xcode.products_dir(derived, config, platform) / f"{scheme}.app", self.bundle_id, platform)
            Path(kwargs["log_path"]).open("a").write("** BUILD SUCCEEDED **\n")
            return _done(0)
        finally:
            with self.lock:
                self.active_builds -= 1


def _done(code, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _matches(call, prefix):
    return all(part in call for part in prefix)


@pytest.fixture
def settings(tmp_path):
    return BuildSettings(root=str(tmp_path / "aqa"), repo=SOURCE, project="fixtures/PlantedBugs",
                         lock_root=str(tmp_path / "locks"), lock_timeout_s=5)


# Settings --------------------------------------------------------------------


def test_settings_from_mapping_and_defaults(tmp_path):
    s = BuildSettings.from_mapping({
        "repo": "/src/app", "project": "apps/App", "scheme": "App", "root": str(tmp_path),
        "ios": {"command": "./build.sh ios", "xcodebuild_args": ["-quiet"]},
        "macos": {"scheme": "App macOS"},
    })
    assert s.ios.command == "./build.sh ios" and s.ios.xcodebuild_args == ["-quiet"]
    assert s.macos.scheme == "App macOS"
    assert s.checkouts_dir() == tmp_path / "checkouts"
    assert s.repos_dir() == tmp_path / "repos"
    assert s.derived_data_dir() == tmp_path / "DerivedData"
    default = BuildSettings()
    assert default.configuration == "Debug" and default.signing == "adhoc"


def test_settings_reject_unknown_and_bad_values():
    with pytest.raises(BuildConfigError) as info:
        BuildSettings.from_mapping({"schemee": "x", "timeout_s": 0, "ios": {"cmd": "x"}, "signing": "team"})
    text = str(info.value)
    assert "build.schemee: unknown key" in text
    assert "build.ios.cmd: unknown key" in text
    assert "build.timeout_s" in text and "build.signing" in text


def test_settings_from_config_falls_back_to_source_dir():
    config = CampaignConfig()
    config.app.source_dir = "/src/app"
    assert BuildSettings.from_config(config).repo == "/src/app"

    class WithBuild:
        app = config.app
        build = type("B", (), {"settings": {"repo": "git@x:y/z.git"}})()

    assert BuildSettings.from_config(WithBuild()).repo == "git@x:y/z.git"


def test_repo_name_for():
    assert repo_name_for("git@github.com:simonrab/SwarmQA.git") == "SwarmQA"
    assert repo_name_for("https://github.com/acme/App") == "App"
    assert repo_name_for("/Users/me/src/my app/") == "my-app"


# Xcode helpers ---------------------------------------------------------------


def test_parse_recorded_list_and_pick_scheme(tmp_path):
    info = xcode.parse_list(LIST_FIXTURE)
    assert info["schemes"] == ["PlantedBugs"]
    project = xcode.XcodeProject("-project", tmp_path / "PlantedBugs.xcodeproj")
    assert xcode.pick_scheme(info, project) == "PlantedBugs"


def test_pick_scheme_from_workspace_with_noise(tmp_path):
    info = xcode.parse_list(LIST_WORKSPACE)
    workspace = xcode.XcodeProject("-workspace", tmp_path / "Shop.xcworkspace")
    # No scheme named Shop; the only non-test, non-Pods scheme is ambiguous with ShopKit.
    with pytest.raises(xcode.DetectError, match="set build.scheme"):
        xcode.pick_scheme(info, workspace)
    info["schemes"].remove("ShopKit")
    assert xcode.pick_scheme(info, workspace) == "ShopApp"


def test_find_project_prefers_workspace_and_honours_setting(tmp_path):
    (tmp_path / "App.xcodeproj").mkdir()
    assert xcode.find_project(tmp_path, None).flag == "-project"
    (tmp_path / "App.xcworkspace").mkdir()
    found = xcode.find_project(tmp_path, None)
    assert found.flag == "-workspace" and found.path.name == "App.xcworkspace"
    assert xcode.find_project(tmp_path, "App.xcodeproj").flag == "-project"
    with pytest.raises(xcode.DetectError, match="not a directory"):
        xcode.find_project(tmp_path, "nowhere")
    (tmp_path / "Other.xcodeproj").mkdir()
    (tmp_path / "App.xcworkspace").rmdir()
    with pytest.raises(xcode.DetectError, match="several projects"):
        xcode.find_project(tmp_path, None)


def test_xcodebuild_args_signing_and_destinations(tmp_path):
    project = xcode.XcodeProject("-project", tmp_path / "A.xcodeproj")
    ios = xcode.xcodebuild_args(project, "A", "ios", configuration="Debug", derived_data=tmp_path / "dd")
    assert ios[ios.index("-destination") + 1] == "generic/platform=iOS Simulator"
    assert "CODE_SIGN_IDENTITY=-" in ios and "CODE_SIGN_STYLE=Manual" in ios and "DEVELOPMENT_TEAM=" in ios
    assert any(arg.startswith("ARCHS=") for arg in ios) and ios[-1] == "build"
    mac = xcode.xcodebuild_args(project, "A", "macos", configuration="Debug", derived_data=tmp_path / "dd",
                                signing="project")
    assert mac[mac.index("-destination") + 1] == "platform=macOS"
    assert "CODE_SIGN_IDENTITY=-" not in mac and not any(arg.startswith("ARCHS=") for arg in mac)


def test_bundle_id_from_xml_and_binary_plists(tmp_path):
    ios = make_app(tmp_path / "A.app", "dev.a", "ios")
    mac = make_app(tmp_path / "B.app", "dev.b", "macos", binary=True)
    assert xcode.read_bundle_id(ios) == "dev.a" and xcode.app_platform(ios) == "ios"
    assert xcode.read_bundle_id(mac) == "dev.b" and xcode.app_platform(mac) == "macos"
    empty = tmp_path / "C.app"
    empty.mkdir()
    with pytest.raises(xcode.DetectError):
        xcode.read_bundle_id(empty)


def test_apps_in_output_and_settings_products(tmp_path):
    app = make_app(tmp_path / "out" / "My App.app", "dev.x", "ios")
    plain = make_app(tmp_path / "out" / "Plain.app", "dev.y", "ios")
    text = f"building...\nios={plain}\nrelative: out/Plain.app\n"
    assert xcode.apps_in_output(text, tmp_path) == [plain]
    settings = json.dumps([
        {"target": "Kit", "buildSettings": {"WRAPPER_EXTENSION": "framework", "TARGET_BUILD_DIR": "/x",
                                             "FULL_PRODUCT_NAME": "Kit.framework"}},
        {"target": "App", "buildSettings": {"PRODUCT_TYPE": "com.apple.product-type.application",
                                             "TARGET_BUILD_DIR": str(app.parent), "FULL_PRODUCT_NAME": app.name}},
    ])
    assert xcode.products_from_settings("noise\n" + settings) == [app]


# Checkouts -------------------------------------------------------------------


def test_checkout_clones_once_and_reuses(settings):
    fake = FakeRunner()
    cache = RepoCache(SOURCE, settings, runner=fake)
    first, lock = cache.checkout("main")
    assert lock is None and not first.reused
    assert first.sha == SHA_A
    assert first.path == settings.checkouts_dir() / "PlantedBugs" / SHA_A
    assert fake.count("git", "clone", "--mirror") == 1
    second, _ = cache.checkout(SHA_A)
    assert second.reused and second.path == first.path
    assert fake.count("clone") == 1 and fake.count("worktree", "add") == 1
    # Every git call is non-interactive.
    assert all(kw.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0"
               for call, kw in zip(fake.calls, fake.kwargs) if call[0] == "git")


def test_checkout_fetches_missing_sha(settings):
    fake = FakeRunner(remote_only={"feature": SHA_B})
    cache = RepoCache(SOURCE, settings, runner=fake)
    got, _ = cache.checkout(SHA_B[:10])
    assert got.sha == SHA_B
    assert fake.count("fetch") >= 1


def test_checkout_unknown_ref_fails_cleanly(settings):
    cache = RepoCache(SOURCE, settings, runner=FakeRunner())
    with pytest.raises(BuildFailed) as info:
        cache.checkout("nope")
    assert info.value.failure.stage == "checkout" and "not found" in info.value.failure.message


def test_broken_checkout_is_recreated(settings):
    fake = FakeRunner()
    cache = RepoCache(SOURCE, settings, runner=fake)
    first, _ = cache.checkout(SHA_A)
    (first.path / ".git").unlink()
    again, _ = cache.checkout(SHA_A)
    assert not again.reused and fake.count("worktree", "add") == 2


def test_local_source_is_cloned_without_hardlinks(tmp_path, settings):
    source = tmp_path / "src"
    source.mkdir()
    fake = FakeRunner()
    cache = RepoCache(str(source), settings, runner=fake)
    cache.checkout("HEAD")
    clone = next(call for call in fake.calls if "clone" in call)
    assert "--no-hardlinks" in clone and str(source.resolve()) in clone
    assert cache.name == "src"


def test_prune_keeps_recent_and_skips_busy(settings):
    fake = FakeRunner(commits={"HEAD": SHA_A, "b": SHA_B, "c": "c" * 40})
    cache = RepoCache(SOURCE, settings, runner=fake)
    paths = {}
    for index, sha in enumerate((SHA_A, SHA_B, "c" * 40)):
        checkout, _ = cache.checkout(sha)
        stamp = 1_000_000 + index * 100
        os.utime(checkout.path / ".git", (stamp, stamp))
        paths[sha] = checkout.path
    (settings.artifacts_dir() / "PlantedBugs" / SHA_A / "ios").mkdir(parents=True)
    busy = cache.sha_lock(SHA_B)
    assert busy.try_acquire()
    try:
        removed = cache.prune(keep=1)
    finally:
        busy.release()
    assert removed == [paths[SHA_A]]
    assert not paths[SHA_A].exists() and paths[SHA_B].exists() and paths["c" * 40].exists()
    assert not (settings.artifacts_dir() / "PlantedBugs" / SHA_A).exists()
    # max_age removes anything older, even inside `keep`.
    removed = cache.prune(keep=10, max_age_s=50, now=1_000_000 + 200 + 60)
    assert set(removed) == {paths[SHA_B], paths["c" * 40]}


def test_concurrent_checkouts_of_one_sha_clone_once(settings):
    fake = FakeRunner()
    results = []

    def work():
        results.append(RepoCache(SOURCE, settings, runner=fake).checkout(SHA_A)[0])

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and {item.sha for item in results} == {SHA_A}
    assert fake.count("clone") == 1 and fake.count("worktree", "add") == 1
    assert sum(1 for item in results if not item.reused) == 1


# Building --------------------------------------------------------------------


def test_auto_detect_builds_both_platforms(settings):
    fake = FakeRunner()
    builder = ShaBuilder(settings, runner=fake)
    report = builder.build_many("main", ("ios", "macos"))
    assert report.ok, report.failures
    ios, mac = report.artifacts
    assert (ios.platform, mac.platform) == ("ios", "macos")
    assert ios.sha == mac.sha == SHA_A
    assert ios.bundle_id == mac.bundle_id == "dev.swarmqa.PlantedBugs"
    assert Path(ios.app_path) == settings.artifacts_dir() / "PlantedBugs" / SHA_A / "ios" / "app" / "PlantedBugs.app"
    assert xcode.app_platform(Path(mac.app_path)) == "macos"
    assert Path(ios.log_path).read_text().count("BUILD SUCCEEDED") == 1
    # -list runs once for both platforms; builds use per-repo DerivedData in the project dir.
    assert fake.count("xcodebuild", "-list") == 1
    build = next(call for call in fake.calls if call[0] == "xcodebuild" and call[-1] == "build")
    assert build[build.index("-derivedDataPath") + 1] == str(settings.derived_data_dir() / "PlantedBugs")
    assert build[build.index("-project") + 1].endswith("fixtures/PlantedBugs/PlantedBugs.xcodeproj")
    assert "-scheme" in build and build[build.index("-scheme") + 1] == "PlantedBugs"


def test_cached_artifact_is_reused_and_force_rebuilds(settings):
    fake = FakeRunner()
    builder = ShaBuilder(settings, runner=fake)
    first = builder.build_result(SHA_A, "ios")
    again = builder.build_result(SHA_A, "ios")
    assert again.reused and again.artifact == first.artifact
    builds = sum(1 for call in fake.calls if call[0] == "xcodebuild" and call[-1] == "build")
    assert builds == 1
    forced = builder.build_result(SHA_A, "ios", force=True)
    assert not forced.reused and forced.ok
    assert sum(1 for call in fake.calls if call[0] == "xcodebuild" and call[-1] == "build") == 2


def test_scheme_override_skips_list(settings):
    settings.scheme = "PlantedBugs"
    fake = FakeRunner()
    assert ShaBuilder(settings, runner=fake).build_result(SHA_A, "macos").ok
    assert fake.count("-list") == 0


def test_command_mode_uses_printed_app(settings):
    def shell(command, kwargs):
        assert command == "./build.sh ios"
        assert kwargs["cwd"].endswith(SHA_A)
        env = kwargs["env"]
        assert env["AQA_PLATFORM"] == "ios" and env["AQA_SHA"] == SHA_A
        app = make_app(Path(kwargs["cwd"]) / "out" / "Custom.app", "dev.custom", "ios")
        Path(kwargs["log_path"]).open("a").write(f"lots of output\nios={app}\n")
        return _done(0)

    settings.ios.command = "./build.sh ios"
    fake = FakeRunner(shell=shell)
    artifact = ShaBuilder(settings, runner=fake).build(SHA_A, "ios")
    assert artifact.bundle_id == "dev.custom" and artifact.app_path.endswith("/ios/app/Custom.app")
    assert fake.count("xcodebuild") == 0


def test_command_mode_finds_left_app_and_reports_missing(settings):
    def leaves_app(command, kwargs):
        make_app(Path(kwargs["cwd"]) / "build" / "macos" / "Left.app", "dev.left", "macos")
        return _done(0, "done\n")

    settings.command = "make app"
    artifact = ShaBuilder(settings, runner=FakeRunner(shell=leaves_app)).build(SHA_A, "macos")
    assert artifact.bundle_id == "dev.left"

    settings.repo_name = "other"
    nothing = ShaBuilder(settings, runner=FakeRunner(shell=lambda c, k: _done(0))).build_result(SHA_A, "macos")
    assert nothing.failure.stage == "product"


def test_build_failure_carries_log_tail_and_becomes_finding(settings):
    settings.scheme = "Bogus"
    fake = FakeRunner(fail_build="Bogus")
    builder = ShaBuilder(settings, runner=fake)
    with pytest.raises(BuildFailed) as info:
        builder.build(SHA_A, "ios")
    failure = info.value.failure
    assert failure.stage == "build" and failure.platform == "ios" and failure.sha == SHA_A
    assert "exit 65" in failure.message
    assert "** BUILD FAILED **" in failure.log_tail and "error: cannot find 'bar'" in failure.log_tail
    assert Path(failure.log_path).is_file()

    finding = build_failure_finding(failure)
    assert isinstance(finding, Finding)
    assert finding.severity == "critical" and finding.kind == "launch"
    assert finding.category == category_for_kind(finding.kind)
    assert finding.id == f"build-ios-{SHA_A[:12]}"
    assert "BUILD FAILED" in finding.details and finding.environment["stage"] == "build"
    # Round-trips through findings.json.
    assert Finding.from_dict(json.loads(json.dumps(finding.__dict__, default=lambda o: o.__dict__))).id == finding.id
    # Same error on another SHA dedups to one fingerprint.
    other = BuildFailure(failure.repo, SHA_B, "ios", "build", failure.message, failure.log_path, failure.log_tail)
    assert build_failure_finding(other).fingerprint == finding.fingerprint
    # And the report-level helper.
    report = builder.build_many(SHA_A, ("ios",))
    assert not report.ok and [item.kind for item in report.findings()] == ["launch"]


def test_detect_failure_is_structured(settings):
    settings.project = "missing/dir"
    result = ShaBuilder(settings, runner=FakeRunner()).build_result(SHA_A, "ios")
    assert result.failure.stage == "detect" and "build.project" in result.failure.message


def test_checkout_failure_is_structured(settings):
    result = ShaBuilder(settings, runner=FakeRunner()).build_result("deadbeef00", "macos")
    assert result.failure.stage == "checkout" and result.failure.platform == "macos"
    assert build_failure_finding(result.failure).severity == "critical"


def test_codesign_failure_is_reported(settings):
    class BadSign(FakeRunner):
        def __call__(self, args, **kwargs):
            if args[0] == "codesign":
                return _done(1, "", "resource fork, Finder information, or similar detritus not allowed")
            return super().__call__(args, **kwargs)

    result = ShaBuilder(settings, runner=BadSign()).build_result(SHA_A, "macos")
    assert result.failure.stage == "codesign" and "detritus" in result.failure.message


def test_builds_of_one_repo_serialise_on_derived_data(settings):
    fake = FakeRunner(commits={"HEAD": SHA_A, "b": SHA_B}, delay=0.2)
    settings.scheme = "PlantedBugs"
    results = []

    def work(sha):
        results.append(ShaBuilder(settings, runner=fake).build_result(sha, "ios"))

    threads = [threading.Thread(target=work, args=(sha,)) for sha in (SHA_A, SHA_B)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(item.ok for item in results) and {item.sha for item in results} == {SHA_A, SHA_B}
    assert fake.max_active_builds == 1
    assert Path(results[0].artifact.app_path) != Path(results[1].artifact.app_path)


def test_log_excerpt_puts_errors_before_tail(tmp_path):
    log = tmp_path / "b.log"
    log.write_text("x.swift:1:1: error: boom\n" + "".join(f"line {n}\n" for n in range(100)))
    excerpt = log_excerpt(log, lines=5)
    assert excerpt.splitlines()[0] == "x.swift:1:1: error: boom"
    assert excerpt.splitlines()[-1] == "line 99"


# The verify seam -------------------------------------------------------------


def test_verify_adapter_builds_and_points_config_at_app(settings, tmp_path):
    fake = FakeRunner()
    config = CampaignConfig()
    config.app.platform = "ios"
    builder = verify_builder(settings, runner=fake)
    outcome = builder(config, tmp_path / "verify" / "build.log")
    assert isinstance(outcome, BuildOutcome) and outcome.ok and not outcome.skipped
    assert config.app.path.endswith("/ios/app/PlantedBugs.app")
    assert config.app.bundle_id == "dev.swarmqa.PlantedBugs"
    assert Path(outcome.log).read_text().count("BUILD SUCCEEDED") == 1
    assert SHA_A[:12] in outcome.message


def test_verify_adapter_failure_and_fallback(settings, tmp_path):
    settings.scheme = "Bogus"
    config = CampaignConfig()
    outcome = verify_builder(settings, runner=FakeRunner(fail_build="Bogus"))(config, tmp_path / "b.log")
    assert not outcome.ok and "BUILD FAILED" in outcome.message and outcome.log
    # No repo anywhere: behaves like the old build_app (skips without a build command).
    bare = BuildSettings(root=str(tmp_path / "x"))
    skipped = verify_builder(bare, runner=FakeRunner())(CampaignConfig(), tmp_path / "c.log")
    assert skipped.ok and skipped.skipped


def test_verify_adapter_local_repo_refuses_dirty_tree(tmp_path, settings):
    top = tmp_path / "work"
    (top / "fixtures" / "PlantedBugs").mkdir(parents=True)

    class Local(FakeRunner):
        dirty = " M fixtures/PlantedBugs/App.swift\n"

        def __call__(self, args, **kwargs):
            if args[:2] == ["git", "rev-parse"] and "--show-toplevel" in args:
                return _done(0, f"{top}\n")
            if args[:2] == ["git", "status"]:
                return _done(0, self.dirty)
            if args[:2] == ["git", "rev-parse"] and "--verify" in args and kwargs.get("cwd") == str(top):
                return _done(0, SHA_A + "\n")
            return super().__call__(args, **kwargs)

    config = CampaignConfig()
    config.app.platform = "macos"
    config.app.source_dir = str(top / "fixtures" / "PlantedBugs")
    local = BuildSettings(root=settings.root, lock_root=settings.lock_root)
    runner = Local()
    outcome = verify_builder(local, runner=runner)(config, tmp_path / "b.log")
    assert not outcome.ok and "uncommitted changes" in outcome.message
    assert "fixtures/PlantedBugs/App.swift" in outcome.message
    runner.dirty = ""
    outcome = verify_builder(local, runner=runner)(config, tmp_path / "b.log")
    assert outcome.ok, outcome.message
    # The source_dir subdirectory became the project dir.
    build = next(call for call in runner.calls if call[0] == "xcodebuild" and call[-1] == "build")
    assert "fixtures/PlantedBugs/PlantedBugs.xcodeproj" in build[build.index("-project") + 1]


def test_verify_core_accepts_adapter(settings, tmp_path):
    from swarmqa.verify import build as build_mod

    adapter = verify_builder(settings, runner=FakeRunner())
    # Structural check against the Builder alias: (config, log_path) -> BuildOutcome.
    fn: build_mod.Builder = adapter
    config = CampaignConfig()
    config.app.platform = "macos"
    assert fn(config, tmp_path / "l.log").ok


# CLI -------------------------------------------------------------------------


def test_cli_builds_and_prints_json(settings, tmp_path, capsys, monkeypatch):
    from swarmqa.build import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AQA_HOME", settings.root)
    code = cli.main(["--sha", "main", "--repo", SOURCE, "--project", "fixtures/PlantedBugs", "--json"],
                    runner=FakeRunner())
    document = json.loads(capsys.readouterr().out)
    assert code == 0 and document["ok"] and len(document["results"]) == 2
    code = cli.main(["--sha", "main", "--repo", SOURCE, "--scheme", "Bogus", "--platform", "ios", "--force"],
                    runner=FakeRunner(fail_build="Bogus"))
    assert code == 1 and "FAILED" in capsys.readouterr().err
