"""Build one SHA of a repo for iOS Simulator and/or macOS.

`ShaBuilder(settings).build(sha, "ios")` checks the SHA out (see
`checkout.py`), builds it, copies the `.app` to
`builds/<repo>/<sha>/<platform>/app/`, and returns a `BuildArtifact`.
A second call for the same SHA and platform returns the stored artifact
without building (pass `force=True` to rebuild).

Two modes per platform:

- **command**: `build.<platform>.command` (or `build.command`) runs through
  `/bin/sh -c` in the checkout. It must print the `.app` path or leave a
  `.app` in the checkout or `$AQA_DERIVED_DATA`. It gets `AQA_PLATFORM`,
  `AQA_SHA`, `AQA_CHECKOUT`, `AQA_DERIVED_DATA`, `AQA_CONFIGURATION` and
  `AQA_OUTPUT_DIR`.
- **auto-detect**: find the project (`build.project` or the only one in the
  checkout), pick the scheme with `xcodebuild -list -json` unless
  `build.scheme` is set, and run xcodebuild with DerivedData
  `DerivedData/<repo>`, ad-hoc signing, and `generic/platform=iOS Simulator`
  or `platform=macOS`.

DerivedData is shared by every SHA of a repo, so builds of one repo hold the
host lock `build-dd-<repo>` while xcodebuild runs and the product is copied.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from swarmqa.build import xcode
from swarmqa.build.checkout import Checkout, RepoCache
from swarmqa.build.errors import BuildFailed, BuildFailure, log_excerpt
from swarmqa.build.runner import Runner, build_runner, run
from swarmqa.build.settings import PLATFORMS, BuildSettings
from swarmqa.devices.locks import HostLock, LockTimeout
from swarmqa.models import BuildArtifact, Finding

ARTIFACT_JSON = "artifact.json"
LIST_TIMEOUT_S = 300.0


@dataclass
class BuildResult:
    """One platform's outcome. Exactly one of `artifact` and `failure` is set."""

    platform: str
    sha: str
    artifact: BuildArtifact | None = None
    failure: BuildFailure | None = None
    duration_s: float = 0.0
    reused: bool = False
    mode: str = ""

    @property
    def ok(self) -> bool:
        return self.artifact is not None


@dataclass
class BuildReport:
    sha: str
    results: list[BuildResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.results)

    @property
    def artifacts(self) -> list[BuildArtifact]:
        return [item.artifact for item in self.results if item.artifact is not None]

    @property
    def failures(self) -> list[BuildFailure]:
        return [item.failure for item in self.results if item.failure is not None]

    def findings(self, **kwargs) -> list[Finding]:
        from swarmqa.build.findings import build_failure_finding

        return [build_failure_finding(item, **kwargs) for item in self.failures]


@dataclass
class _Plan:
    mode: str
    command: str | None = None
    project: xcode.XcodeProject | None = None
    scheme: str | None = None


class ShaBuilder:
    """Per-SHA builder. Every git and xcodebuild call goes through `runner`."""

    def __init__(
        self,
        settings: BuildSettings | None = None,
        *,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.settings = settings or BuildSettings()
        self.runner = runner or build_runner
        self.clock = clock
        self.wall = wall
        self._schemes: dict[tuple[str, str], str] = {}

    # Public API -------------------------------------------------------------

    def repo_cache(self, repo: str | None = None) -> RepoCache:
        source = repo or self.settings.repo
        if not source:
            raise BuildFailed(BuildFailure("", "", "", "checkout", "no repo to build: set build.repo or pass repo"))
        return RepoCache(source, self.settings, runner=self.runner)

    def build(self, sha: str, platform: str, *, repo: str | None = None, force: bool = False) -> BuildArtifact:
        """Build and return the artifact, or raise `BuildFailed`."""
        result = self.build_result(sha, platform, repo=repo, force=force)
        if result.failure is not None:
            raise BuildFailed(result.failure)
        assert result.artifact is not None
        return result.artifact

    def build_many(self, sha: str, platforms: list[str] | tuple[str, ...] = PLATFORMS, *,
                   repo: str | None = None, force: bool = False) -> BuildReport:
        """Build each platform in turn; one failing does not stop the others."""
        report = BuildReport(sha)
        for platform in platforms:
            result = self.build_result(sha, platform, repo=repo, force=force)
            report.results.append(result)
            report.sha = result.sha or report.sha
        return report

    def build_result(self, sha: str, platform: str, *, repo: str | None = None, force: bool = False) -> BuildResult:
        """Build one platform and report the outcome. Never raises BuildFailed."""
        started = self.clock()
        result = BuildResult(platform, sha)
        try:
            self.settings.platform(platform)
            cache = self.repo_cache(repo)
            checkout, sha_lock = cache.checkout(sha, hold=True)
        except BuildFailed as exc:
            exc.failure.platform = platform
            exc.failure.sha = exc.failure.sha or sha
            result.failure = exc.failure
            result.duration_s = round(self.clock() - started, 3)
            return result
        except Exception as exc:  # noqa: BLE001 - settings errors and the like
            result.failure = BuildFailure("", sha, platform, "checkout", f"{type(exc).__name__}: {exc}")
            result.duration_s = round(self.clock() - started, 3)
            return result
        result.sha = checkout.sha
        try:
            self._build_checkout(cache, checkout, platform, force, result)
        finally:
            if sha_lock is not None:
                sha_lock.release()
            result.duration_s = round(self.clock() - started, 3)
        return result

    def prune(self, repo: str | None = None, *, keep: int | None = None,
              max_age_s: float | None = None) -> list[Path]:
        return self.repo_cache(repo).prune(keep=keep, max_age_s=max_age_s)

    def artifact_dir(self, repo_name: str, sha: str, platform: str) -> Path:
        return self.settings.artifacts_dir() / repo_name / sha / platform

    def stored_artifact(self, repo_name: str, sha: str, platform: str) -> BuildArtifact | None:
        """The artifact a previous build left, if its app is still there."""
        path = self.artifact_dir(repo_name, sha, platform) / ARTIFACT_JSON
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            artifact = BuildArtifact(platform=data["platform"], app_path=data["app_path"],
                                     bundle_id=data["bundle_id"], sha=data.get("sha", sha),
                                     log_path=data.get("log_path"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        app = Path(artifact.app_path)
        if not app.is_dir() or xcode.app_platform(app) != platform:
            return None
        return artifact

    # One build ----------------------------------------------------------------

    def _build_checkout(self, cache: RepoCache, checkout: Checkout, platform: str, force: bool,
                        result: BuildResult) -> None:
        name, sha = checkout.repo_name, checkout.sha
        if not force:
            stored = self.stored_artifact(name, sha, platform)
            if stored is not None:
                result.artifact, result.reused, result.mode = stored, True, "cached"
                return
        out_dir = self.artifact_dir(name, sha, platform)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / ARTIFACT_JSON).unlink(missing_ok=True)
        log_path = out_dir / "build.log"
        log_path.write_text(f"# swarmqa build: {name} {sha} {platform}\n# checkout: {checkout.path}\n",
                            encoding="utf-8")

        def fail(stage: str, message: str) -> None:
            result.failure = BuildFailure(name, sha, platform, stage, message, str(log_path),
                                          log_excerpt(log_path))

        try:
            plan = self._plan(checkout, platform, log_path)
        except xcode.DetectError as exc:
            _append(log_path, f"# detect failed: {exc}\n")
            return fail("detect", str(exc))
        result.mode = plan.mode
        derived = self.settings.derived_data_dir() / name
        lock = HostLock(f"build-dd-{name}", root=self.settings.lock_root)
        try:
            lock.acquire(self.settings.lock_timeout_s, poll_s=0.5)
        except LockTimeout as exc:
            return fail("build", f"timed out waiting for DerivedData {derived}: {exc}")
        try:
            product, stage, message = self._run_plan(plan, checkout, platform, derived, out_dir, log_path)
            if product is None:
                return fail(stage, message)
            app, error = self._store_product(product, out_dir, log_path)
        finally:
            lock.release()
        if app is None:
            return fail("product", error)
        if self.settings.verify_signature:
            problem = self._verify_signature(app, log_path)
            if problem:
                return fail("codesign", problem)
        try:
            bundle_id = xcode.read_bundle_id(app)
        except xcode.DetectError as exc:
            return fail("product", str(exc))
        artifact = BuildArtifact(platform=platform, app_path=str(app), bundle_id=bundle_id, sha=sha,
                                 log_path=str(log_path))
        record = asdict(artifact) | {"repo": name, "mode": plan.mode, "scheme": plan.scheme,
                                     "built_at": self.wall()}
        (out_dir / ARTIFACT_JSON).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        _append(log_path, f"# built {app} ({bundle_id})\n")
        result.artifact = artifact

    def _plan(self, checkout: Checkout, platform: str, log_path: Path) -> _Plan:
        options = self.settings.platform(platform)
        command = options.command or self.settings.command
        if command:
            return _Plan("command", command=command)
        project = xcode.find_project(checkout.path, options.project or self.settings.project)
        scheme = options.scheme or self.settings.scheme
        if not scheme:
            key = (str(project.path), checkout.sha)
            scheme = self._schemes.get(key)
            if scheme is None:
                _append(log_path, f"$ xcodebuild -list -json {project.flag} {project.path}\n")
                info = xcode.list_project(self.runner, project, timeout=LIST_TIMEOUT_S)
                scheme = self._schemes[key] = xcode.pick_scheme(info, project)
                _append(log_path, f"# schemes: {', '.join(info['schemes'])}; using {scheme}\n")
        return _Plan("auto", project=project, scheme=scheme)

    def _run_plan(self, plan: _Plan, checkout: Checkout, platform: str, derived: Path, out_dir: Path,
                  log_path: Path) -> tuple[Path | None, str, str]:
        options = self.settings.platform(platform)
        configuration = options.configuration or self.settings.configuration
        since = self.wall()
        if plan.mode == "command":
            assert plan.command is not None
            env = {"AQA_PLATFORM": platform, "AQA_SHA": checkout.sha, "AQA_CHECKOUT": str(checkout.path),
                   "AQA_DERIVED_DATA": str(derived), "AQA_CONFIGURATION": configuration,
                   "AQA_OUTPUT_DIR": str(out_dir)}
            _append(log_path, f"$ {plan.command}\n# cwd: {checkout.path}\n")
            offset = log_path.stat().st_size
            done = run(self.runner, ["/bin/sh", "-c", plan.command], cwd=str(checkout.path), env=env,
                       timeout=self.settings.timeout_s, log_path=str(log_path))
            _append_output(log_path, done)
            if not done.ok:
                return None, "build", _exit_message("build command", done.returncode, self.settings.timeout_s)
            printed = _read_from(log_path, offset)
            for path in xcode.apps_in_output(printed, checkout.path):
                if xcode.app_platform(path) == platform:
                    return path, "", ""
            found = xcode.scan_for_apps([checkout.path, derived], platform, since)
            if found:
                return found[0], "", ""
            return None, "product", (f"the build command succeeded but printed no {platform} .app path "
                                     f"and left none in the checkout or $AQA_DERIVED_DATA")
        assert plan.project is not None and plan.scheme is not None
        args = xcode.xcodebuild_args(
            plan.project, plan.scheme, platform, configuration=configuration, derived_data=derived,
            destination=options.destination, archs=options.archs, signing=self.settings.signing,
            extra=[*self.settings.xcodebuild_args, *options.xcodebuild_args],
        )
        _append(log_path, "$ " + " ".join(_quote(arg) for arg in args) + "\n")
        done = run(self.runner, args, cwd=str(plan.project.path.parent), timeout=self.settings.timeout_s,
                   log_path=str(log_path))
        _append_output(log_path, done)
        if not done.ok:
            return None, "build", _exit_message("xcodebuild", done.returncode, self.settings.timeout_s)
        product = xcode.find_product(xcode.products_dir(derived, configuration, platform), platform,
                                     scheme=plan.scheme, since=since)
        if product is not None:
            return product, "", ""
        shown = run(self.runner, xcode.xcodebuild_args(
            plan.project, plan.scheme, platform, configuration=configuration, derived_data=derived,
            destination=options.destination, action="-showBuildSettings",
        ) + ["-json"], cwd=str(plan.project.path.parent), timeout=LIST_TIMEOUT_S)
        for path in xcode.products_from_settings(shown.stdout):
            if path.is_dir() and xcode.app_platform(path) == platform:
                return path, "", ""
        return None, "product", f"xcodebuild succeeded but no {platform} .app was found for scheme {plan.scheme}"

    def _store_product(self, product: Path, out_dir: Path, log_path: Path) -> tuple[Path | None, str]:
        """Copy the app out of the checkout or shared DerivedData, without extended attributes."""
        app_dir = out_dir / "app"
        destination = app_dir / product.name
        try:
            if product.resolve().is_relative_to(app_dir.resolve()):
                return product, ""
        except OSError:
            pass
        shutil.rmtree(app_dir, ignore_errors=True)
        app_dir.mkdir(parents=True, exist_ok=True)
        copied = run(self.runner, ["ditto", "--noextattr", "--noqtn", str(product), str(destination)],
                     timeout=600)
        if copied.returncode == 127:
            try:
                shutil.copytree(product, destination, symlinks=True)
            except OSError as exc:
                return None, f"could not copy {product}: {exc}"
        elif not copied.ok:
            return None, f"could not copy {product}: {copied.detail()}"
        if sys.platform == "darwin":
            run(self.runner, ["xattr", "-cr", str(destination)], timeout=120)
        _append(log_path, f"# copied {product} -> {destination}\n")
        return destination, ""

    def _verify_signature(self, app: Path, log_path: Path) -> str:
        checked = run(self.runner, ["codesign", "--verify", "--strict", str(app)], timeout=120)
        if checked.returncode == 127:
            return ""
        if checked.ok:
            _append(log_path, f"# codesign --verify ok: {app}\n")
            return ""
        detail = checked.detail()
        _append(log_path, f"# codesign --verify failed: {detail}\n")
        return f"codesign --verify failed for {app}: {detail}"


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _append_output(path: Path, done) -> None:
    # A runner that captured output instead of writing the log (tests, or a custom runner).
    text = (done.stdout or "") + (done.stderr or "")
    if text:
        _append(path, text if text.endswith("\n") else text + "\n")


def _read_from(path: Path, offset: int) -> str:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", "replace")


def _exit_message(what: str, code: int, timeout: float) -> str:
    if code == 124:
        return f"{what} timed out after {timeout:g}s"
    if code == 127:
        return f"{what} could not start (not found)"
    return f"{what} failed (exit {code})"


def _quote(arg: str) -> str:
    return arg if arg and all(ch.isalnum() or ch in "-_=./:+,@" for ch in arg) else json.dumps(arg)
