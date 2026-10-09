"""Settings for the per-SHA builder: the `[build]` config table.

`BuildSettings.from_mapping` takes the table as written in TOML, for example

    [build]
    repo = "git@github.com:acme/App.git"   # URL or local path; default: app.source_dir
    project = "apps/App"                    # dir, .xcodeproj or .xcworkspace, relative to the checkout
    scheme = "App"
    configuration = "Debug"

    [build.ios]
    command = "./scripts/build-sim.sh"      # optional: skip auto-detect for this platform

    [build.macos]
    scheme = "App (macOS)"

Unknown keys raise `BuildConfigError`, so a typo never silently falls back
to auto-detect. Every directory defaults under `root` (`~/.aqa`), which is
kept outside iCloud-synced folders on purpose (see docs/build.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from swarmqa.errors import ConfigError

Signing = Literal["adhoc", "project"]
PLATFORMS = ("ios", "macos")
DEFAULT_ROOT = "~/.aqa"
ROOT_ENV = "AQA_HOME"


class BuildConfigError(ConfigError):
    """The `[build]` table has a bad key or value."""


@dataclass
class PlatformBuild:
    """Per-platform overrides: `[build.ios]` and `[build.macos]`."""

    command: str | None = None
    scheme: str | None = None
    project: str | None = None
    destination: str | None = None
    configuration: str | None = None
    # ARCHS for the build. None means "the host arch" for ios (a generic
    # simulator destination otherwise builds every arch) and unset for macos.
    # "" leaves ARCHS to the project.
    archs: str | None = None
    xcodebuild_args: list[str] = field(default_factory=list)


@dataclass
class BuildSettings:
    repo: str | None = None
    repo_name: str | None = None
    project: str | None = None
    scheme: str | None = None
    configuration: str = "Debug"
    command: str | None = None
    signing: Signing = "adhoc"
    xcodebuild_args: list[str] = field(default_factory=list)
    timeout_s: float = 1800.0
    git_timeout_s: float = 900.0
    lock_timeout_s: float = 3600.0
    submodules: bool = True
    verify_signature: bool = True
    keep: int = 20
    root: str | None = None
    checkout_root: str | None = None
    repo_cache_root: str | None = None
    derived_data_root: str | None = None
    artifact_root: str | None = None
    lock_root: str | None = None
    ios: PlatformBuild = field(default_factory=PlatformBuild)
    macos: PlatformBuild = field(default_factory=PlatformBuild)

    # Paths ------------------------------------------------------------------

    def base(self) -> Path:
        return Path(self.root or os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()

    def checkouts_dir(self) -> Path:
        return _path(self.checkout_root) or self.base() / "checkouts"

    def repos_dir(self) -> Path:
        return _path(self.repo_cache_root) or self.base() / "repos"

    def derived_data_dir(self) -> Path:
        return _path(self.derived_data_root) or self.base() / "DerivedData"

    def artifacts_dir(self) -> Path:
        return _path(self.artifact_root) or self.base() / "builds"

    def platform(self, platform: str) -> PlatformBuild:
        if platform not in PLATFORMS:
            raise BuildConfigError([f"build: unknown platform {platform!r} (expected ios or macos)"])
        return self.ios if platform == "ios" else self.macos

    # Loading ----------------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "BuildSettings":
        """Build settings from the raw `[build]` table. Raises BuildConfigError."""
        data = dict(data or {})
        errors: list[str] = []
        settings = cls()
        for platform in PLATFORMS:
            table = data.pop(platform, None)
            if table is None:
                continue
            if not isinstance(table, dict):
                errors.append(f"build.{platform}: must be a table")
                continue
            setattr(settings, platform, _fill(PlatformBuild(), table, f"build.{platform}", errors))
        _fill(settings, data, "build", errors, skip=set(PLATFORMS))
        if settings.signing not in ("adhoc", "project"):
            errors.append("build.signing: must be one of adhoc, project")
        if not isinstance(settings.keep, int) or settings.keep < 0:
            errors.append("build.keep: must be an integer >= 0")
        for name in ("timeout_s", "git_timeout_s", "lock_timeout_s"):
            value = getattr(settings, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                errors.append(f"build.{name}: must be a number > 0")
        if errors:
            raise BuildConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: Any) -> "BuildSettings":
        """Read `config.build.settings` (the raw table) when the config has one."""
        build = getattr(config, "build", None)
        raw = getattr(build, "settings", build if isinstance(build, dict) else None)
        settings = cls.from_mapping(raw)
        app = getattr(config, "app", None)
        if settings.repo is None and app is not None and getattr(app, "source_dir", None):
            settings.repo = app.source_dir
        return settings


def _path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None



def _fill(target: Any, table: dict[str, Any], prefix: str, errors: list[str], skip: set[str] = frozenset()):
    known = {item.name: item for item in fields(target) if item.name not in skip}
    for key, value in table.items():
        spec = known.get(key)
        if spec is None:
            errors.append(f"{prefix}.{key}: unknown key")
            continue
        problem = _type_problem(value, spec.type)
        if problem:
            errors.append(f"{prefix}.{key}: {problem}")
            continue
        setattr(target, key, list(value) if isinstance(value, list) else value)
    return target


def _type_problem(value: Any, annotation: Any) -> str | None:
    text = str(annotation)
    if "list" in text:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return "must be a list of strings"
        return None
    if text == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if text in ("float", "int"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "must be a number"
        if text == "int" and not isinstance(value, int):
            return "must be an integer"
        return None
    if not isinstance(value, str):
        return "must be a string"
    return None
