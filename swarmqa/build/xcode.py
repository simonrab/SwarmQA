"""Xcode helpers: find the project and scheme, form the xcodebuild call, find the product.

Nothing here runs a process directly: callers pass the runner.
"""

from __future__ import annotations

import json
import platform as host_platform
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarmqa.build.runner import Runner, run

DESTINATIONS = {"ios": "generic/platform=iOS Simulator", "macos": "platform=macOS"}
PRODUCT_SUBDIR = {"ios": "{config}-iphonesimulator", "macos": "{config}"}
# Unsigned simulator builds and run-locally macOS builds need no team.
ADHOC_SIGNING = ["CODE_SIGN_IDENTITY=-", "CODE_SIGN_STYLE=Manual", "DEVELOPMENT_TEAM="]
_TEST_SCHEME = re.compile(r"(UI)?Tests?$|^Pods-|Tests?[ _-]", re.IGNORECASE)


class DetectError(Exception):
    """The project or scheme could not be chosen; the message says which setting to add."""


@dataclass
class XcodeProject:
    flag: str  # "-project" or "-workspace"
    path: Path

    @property
    def name(self) -> str:
        return self.path.stem

    def args(self) -> list[str]:
        return [self.flag, str(self.path)]


def find_project(checkout: Path, setting: str | None) -> XcodeProject:
    """The `.xcworkspace` or `.xcodeproj` named by `setting` (relative to the
    checkout), or the only one in that directory (a workspace wins over a project).
    """
    target = (checkout / setting) if setting else checkout
    if target.suffix in (".xcodeproj", ".xcworkspace"):
        if not target.exists():
            raise DetectError(f"build.project {setting} does not exist in the checkout")
        return XcodeProject("-workspace" if target.suffix == ".xcworkspace" else "-project", target)
    if not target.is_dir():
        raise DetectError(f"build.project {setting} is not a directory in the checkout")
    workspaces = sorted(target.glob("*.xcworkspace"))
    projects = sorted(target.glob("*.xcodeproj"))
    if len(workspaces) == 1:
        return XcodeProject("-workspace", workspaces[0])
    if len(workspaces) > 1:
        names = ", ".join(path.name for path in workspaces)
        raise DetectError(f"several workspaces in {target} ({names}); set build.project")
    if len(projects) == 1:
        return XcodeProject("-project", projects[0])
    if projects:
        names = ", ".join(path.name for path in projects)
        raise DetectError(f"several projects in {target} ({names}); set build.project")
    where = setting or "the checkout root"
    raise DetectError(f"no .xcodeproj or .xcworkspace in {where}; set build.project (or build.<platform>.command)")


def parse_list(text: str) -> dict[str, Any]:
    """Parse `xcodebuild -list -json` output: {"project"|"workspace": {"name", "schemes", ...}}."""
    start = text.find("{")
    if start < 0:
        raise DetectError("xcodebuild -list printed no JSON")
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise DetectError(f"could not parse xcodebuild -list output: {exc}") from exc
    body = data.get("workspace") or data.get("project") or {}
    return {"name": body.get("name", ""), "schemes": list(body.get("schemes") or []),
            "targets": list(body.get("targets") or []), "configurations": list(body.get("configurations") or [])}


def list_project(runner: Runner, project: XcodeProject, *, timeout: float = 300.0) -> dict[str, Any]:
    result = run(runner, ["xcodebuild", "-list", "-json", *project.args()],
                 cwd=str(project.path.parent), timeout=timeout)
    if not result.ok:
        raise DetectError(f"xcodebuild -list failed: {result.detail()}")
    return parse_list(result.stdout)


def pick_scheme(info: dict[str, Any], project: XcodeProject) -> str:
    """One scheme: the only one, else the one named after the project, else the
    only non-test scheme. Ambiguity is an error naming the choices."""
    schemes: list[str] = info.get("schemes") or []
    if not schemes:
        raise DetectError(f"{project.path.name} has no shared schemes; set build.scheme")
    if len(schemes) == 1:
        return schemes[0]
    for name in (info.get("name"), project.name):
        if name and name in schemes:
            return name
    apps = [scheme for scheme in schemes if not _TEST_SCHEME.search(scheme)]
    if len(apps) == 1:
        return apps[0]
    raise DetectError(f"several schemes in {project.path.name} ({', '.join(schemes)}); set build.scheme")


def host_arch() -> str:
    machine = host_platform.machine()
    return "arm64" if machine in ("arm64", "aarch64") else machine or "arm64"


def xcodebuild_args(
    project: XcodeProject,
    scheme: str,
    platform: str,
    *,
    configuration: str,
    derived_data: Path,
    destination: str | None = None,
    archs: str | None = None,
    signing: str = "adhoc",
    extra: list[str] | None = None,
    action: str = "build",
) -> list[str]:
    args = ["xcodebuild", *project.args(), "-scheme", scheme, "-configuration", configuration,
            "-destination", destination or DESTINATIONS[platform], "-derivedDataPath", str(derived_data)]
    if action != "build":
        return args + [action]
    if archs is None and platform == "ios":
        archs = host_arch()
    if archs:
        args += [f"ARCHS={archs}", "ONLY_ACTIVE_ARCH=YES"]
    if signing == "adhoc":
        args += ADHOC_SIGNING
    args += ["COMPILER_INDEX_STORE_ENABLE=NO", *(extra or []), "build"]
    return args


# Products -------------------------------------------------------------------


def app_platform(app: Path) -> str | None:
    """`macos` for a bundle with Contents/Info.plist, `ios` for Info.plist at the root."""
    if (app / "Contents" / "Info.plist").is_file():
        return "macos"
    if (app / "Info.plist").is_file():
        return "ios"
    return None


def info_plist(app: Path) -> Path:
    mac = app / "Contents" / "Info.plist"
    return mac if mac.is_file() else app / "Info.plist"


def read_bundle_id(app: Path) -> str:
    """CFBundleIdentifier from the bundle's Info.plist (XML or binary)."""
    path = info_plist(Path(app))
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise DetectError(f"could not read {path}: {exc}") from exc
    bundle_id = data.get("CFBundleIdentifier") if isinstance(data, dict) else None
    if not bundle_id or not isinstance(bundle_id, str):
        raise DetectError(f"{path} has no CFBundleIdentifier")
    return bundle_id


def products_dir(derived_data: Path, configuration: str, platform: str) -> Path:
    return derived_data / "Build" / "Products" / PRODUCT_SUBDIR[platform].format(config=configuration)


def find_product(directory: Path, platform: str, *, scheme: str | None = None,
                 since: float | None = None) -> Path | None:
    """The app a build left in `directory`: the only app, else `<scheme>.app`,
    else the newest one written since `since`."""
    if not directory.is_dir():
        return None
    apps = [path for path in directory.glob("*.app") if app_platform(path) == platform]
    if len(apps) == 1:
        return apps[0]
    if scheme:
        for path in apps:
            if path.stem == scheme:
                return path
    fresh = [path for path in apps if since is None or _mtime(path) >= since - 2]
    return max(fresh, key=_mtime) if fresh else None


def products_from_settings(text: str) -> list[Path]:
    """Application products named by `xcodebuild -showBuildSettings -json`."""
    start = text.find("[")
    try:
        entries = json.loads(text[start:]) if start >= 0 else []
    except json.JSONDecodeError:
        return []
    found: list[Path] = []
    for entry in entries:
        settings = entry.get("buildSettings") or {}
        if settings.get("WRAPPER_EXTENSION") != "app" and "application" not in settings.get("PRODUCT_TYPE", ""):
            continue
        if settings.get("TARGET_BUILD_DIR") and settings.get("FULL_PRODUCT_NAME"):
            found.append(Path(settings["TARGET_BUILD_DIR"]) / settings["FULL_PRODUCT_NAME"])
    return found


_APP_TOKEN = re.compile(r"""(?:^|[\s=:'"])((?:/|\.{0,2}/?)[^\s'"=]*?\.app)/?(?=$|[\s'"])""")


def apps_in_output(text: str, cwd: Path) -> list[Path]:
    """`.app` paths printed in command output, last first, that exist on disk."""
    seen: list[Path] = []
    for line in reversed(text.splitlines()):
        for match in reversed(_APP_TOKEN.findall(line.strip())):
            path = Path(match)
            path = path if path.is_absolute() else cwd / path
            if path.is_dir() and path not in seen:
                seen.append(path)
    return seen


def scan_for_apps(roots: list[Path], platform: str, since: float) -> list[Path]:
    """App bundles for `platform` under `roots` written since `since`, newest first.
    Does not descend into bundles or `.git`."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir() or child.is_symlink() or child.name in (".git", "node_modules"):
                    continue
                if child.suffix == ".app":
                    if app_platform(child) == platform and _mtime(child) >= since - 2:
                        found.append(child)
                    continue
                stack.append(child)
    return sorted(found, key=_mtime, reverse=True)


def _mtime(path: Path) -> float:
    """Newest of the bundle directory and its Info.plist: an incremental
    build rewrites files inside the bundle without touching the directory."""
    newest = 0.0
    for item in (path, info_plist(path)):
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    return newest
