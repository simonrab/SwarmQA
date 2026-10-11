"""Find the app `aqa try` tests: a built `.app`, or an Xcode project it builds first.

Every host command goes through the `runner(args, **kwargs)` seam of
`swarmqa.devices.commands`, so tests run without Xcode or a simulator.
"""

from __future__ import annotations

import json
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from swarmqa.build import xcode
from swarmqa.build.settings import BuildSettings
from swarmqa.devices.commands import Runner, run
from swarmqa.errors import BackendUnavailable

DEFAULT_IPHONE = "iPhone 17"
_UDID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
PROJECT_SUFFIXES = (".xcodeproj", ".xcworkspace")


class TryError(Exception):
    """An expected problem. The message says what went wrong and how to fix it."""


@dataclass
class AppInfo:
    """The app to test. `source_dir` is the repo root when the app was built here."""

    platform: str
    bundle_id: str
    path: str | None = None
    source_dir: str | None = None
    name: str = ""


@dataclass
class ProjectPlan:
    """An Xcode project to build at `sha` (HEAD of the repo at `repo`)."""

    project: xcode.XcodeProject
    repo: Path
    sha: str
    scheme: str
    platform: str
    dirty: bool = False


def read_app(path: Path) -> AppInfo:
    """Platform and bundle id from a `.app` bundle's Info.plist."""
    mac = path / "Contents" / "Info.plist"
    plist = mac if mac.is_file() else path / "Info.plist"
    if not path.is_dir() or not plist.is_file():
        raise TryError(f"{path} is not an app bundle (it has no Info.plist). Pass a built .app, "
                       "an .xcodeproj, an .xcworkspace or a project folder.")
    try:
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise TryError(f"Cannot read {plist}: {exc}") from exc
    bundle_id = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    if not isinstance(bundle_id, str) or not bundle_id:
        raise TryError(f"{plist} has no CFBundleIdentifier. Build the app again and retry.")
    name = path.stem
    if mac.is_file():
        return AppInfo("macos", bundle_id, str(path), name=name)
    platforms = data.get("CFBundleSupportedPlatforms") or []
    if "iPhoneOS" in platforms and "iPhoneSimulator" not in platforms:
        raise TryError(f"{path.name} is built for iPhone devices, not for the Simulator. "
                       "Build it with an iOS Simulator destination and try again.")
    return AppInfo("ios", bundle_id, str(path), name=name)


def has_project(folder: Path) -> bool:
    """True when `folder` holds an .xcodeproj or .xcworkspace."""
    return folder.is_dir() and any(any(folder.glob(f"*{suffix}")) for suffix in PROJECT_SUFFIXES)


def plan_project(target: Path, runner: Runner, *, scheme: str | None = None,
                 platform: str | None = None) -> ProjectPlan:
    """Find the project, its git repo and HEAD, the scheme and the platform to build."""
    target = target.resolve()
    folder, setting = (target.parent, target.name) if target.suffix in PROJECT_SUFFIXES else (target, None)
    if setting is None and not has_project(folder):
        raise TryError(f"No .xcodeproj or .xcworkspace in {folder}. Pass a built .app or a project.")
    try:
        project = xcode.find_project(folder, setting)
    except xcode.DetectError as exc:
        raise TryError(f"{_no_setting(str(exc))}. Pass the .xcodeproj or .xcworkspace you want to test.") from exc
    top = _git(runner, folder, "rev-parse", "--show-toplevel")
    if top is None:
        raise TryError(f"{folder} is not in a git repository. aqa try builds the last commit. Commit your "
                       "project first (git init && git add -A && git commit -m init), or build the app "
                       "in Xcode and pass the .app.")
    repo = Path(top).resolve()
    if not project.path.is_relative_to(repo):
        raise TryError(f"{project.path} is outside its git repository {repo}.")
    sha = _git(runner, repo, "rev-parse", "HEAD")
    if not sha:
        raise TryError(f"{repo} has no commits yet. Commit your project first, then try again.")
    dirty = bool(_git(runner, repo, "status", "--porcelain"))
    if not scheme:
        try:
            scheme = xcode.pick_scheme(xcode.list_project(runner, project), project)
        except xcode.DetectError as exc:
            raise TryError(f"{_no_setting(str(exc))}. Pass --scheme NAME.") from exc
    return ProjectPlan(project, repo, sha, scheme, platform or detect_platform(runner, project, scheme), dirty)


def detect_platform(runner: Runner, project: xcode.XcodeProject, scheme: str) -> str:
    """`ios` when the scheme's app builds for the Simulator, else `macos` when it builds for the Mac.
    Defaults to `ios` when Xcode does not say."""
    done = run(runner, ["xcodebuild", "-showBuildSettings", "-json", *project.args(), "-scheme", scheme],
               cwd=str(project.path.parent), timeout=300)
    supported = supported_platforms(done.stdout) if done.ok else set()
    if "macosx" in supported and "iphonesimulator" not in supported:
        return "macos"
    return "ios"


def supported_platforms(text: str) -> set[str]:
    """SUPPORTED_PLATFORMS of the app targets in `xcodebuild -showBuildSettings -json` output."""
    start = text.find("[")
    try:
        entries = json.loads(text[start:]) if start >= 0 else []
    except json.JSONDecodeError:
        return set()
    found: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        settings = (entry.get("buildSettings") or {}) if isinstance(entry, dict) else {}
        if settings.get("WRAPPER_EXTENSION") == "app" or "application" in settings.get("PRODUCT_TYPE", ""):
            found.update(str(settings.get("SUPPORTED_PLATFORMS", "")).split())
    return found


def build_app(plan: ProjectPlan, settings: BuildSettings, make_builder: Callable[[BuildSettings], Any]) -> AppInfo:
    """Build HEAD for `plan.platform` with the per-SHA builder and return the built app."""
    settings.repo = str(plan.repo)
    settings.scheme = plan.scheme
    settings.project = str(plan.project.path.relative_to(plan.repo))
    result = make_builder(settings).build_result(plan.sha, plan.platform, repo=str(plan.repo))
    if result.failure is not None:
        failure = result.failure
        log = f" Full log: {failure.log_path}." if failure.log_path else ""
        raise TryError(f"The {plan.platform} build of {plan.scheme} failed at {failure.stage}: "
                       f"{failure.first_error or failure.message}.{log} Fix the build in Xcode, commit, "
                       "and run aqa try again.")
    artifact = result.artifact
    return AppInfo(plan.platform, artifact.bundle_id, artifact.app_path, str(plan.repo), Path(artifact.app_path).stem)


def pick_simulator(runner: Runner, wanted: str | None) -> tuple[str, str]:
    """(udid, name) of the iOS Simulator to use: `wanted` (a name or UDID), else an available iPhone."""
    wanted = (wanted or "").strip()
    if _UDID.match(wanted):
        return wanted, wanted
    done = run(runner, ["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=120)
    try:
        document = json.loads(done.stdout) if done.ok else None
    except json.JSONDecodeError:
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("devices"), dict):
        raise TryError("Cannot list iOS Simulators (xcrun simctl failed). Install Xcode, open it once, "
                       "and try again.")
    if wanted:
        from swarmqa.driver.ios import select_simulator

        try:
            return select_simulator(document, wanted), wanted
        except BackendUnavailable as exc:
            raise TryError(f"No available iOS Simulator named {wanted!r}. Run `xcrun simctl list devices "
                           "available` and pass a name or UDID with --simulator.") from exc
    iphones = []
    for runtime, entries in document["devices"].items():
        if "iOS" not in str(runtime) or not isinstance(entries, list):
            continue
        version = tuple(int(part) for part in re.findall(r"\d+", str(runtime)))
        for entry in entries:
            name, udid = str(entry.get("name") or ""), str(entry.get("udid") or "")
            if entry.get("isAvailable") is False or not udid or not name.startswith("iPhone"):
                continue
            booted = str(entry.get("state") or "").lower() == "booted"
            iphones.append((name == DEFAULT_IPHONE, booted, version, name, udid))
    if not iphones:
        raise TryError("No iPhone Simulator is installed. In Xcode open Settings > Components, add an iOS "
                       "Simulator runtime, then try again (or pass --simulator NAME_OR_UDID).")
    best = max(iphones, key=lambda item: item[:3])
    return best[4], best[3]


def runner_state(platform: str) -> str:
    """`ready` (a runner build exists), `build` (build.sh will build it on first use) or `missing`."""
    from swarmqa.driver.runner_client import build_script, find_xctestrun

    if find_xctestrun(platform) is not None:
        return "ready"
    return "build" if build_script().is_file() else "missing"


def _git(runner: Runner, cwd: Path, *args: str) -> str | None:
    done = run(runner, ["git", *args], cwd=str(cwd), timeout=60)
    return done.stdout.strip() if done.ok else None


def _no_setting(message: str) -> str:
    return re.sub(r"; set build\.\w+.*$", "", message)
