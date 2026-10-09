"""Per-SHA builder (WP-C4): check out a commit and build its iOS Simulator and macOS apps.

See docs/build.md. The main entry points:

- `ShaBuilder(settings).build(sha, platform) -> BuildArtifact` (raises `BuildFailed`)
- `ShaBuilder.build_many(sha, platforms) -> BuildReport` (never raises; failures carried)
- `build_failure_finding(failure) -> Finding` (critical, for the campaign report)
- `verify_builder(...)` adapts the builder to `swarmqa.verify.build.Builder`
"""

from __future__ import annotations

from swarmqa.build.builder import BuildReport, BuildResult, ShaBuilder
from swarmqa.build.checkout import Checkout, RepoCache, repo_name_for
from swarmqa.build.errors import BuildFailed, BuildFailure
from swarmqa.build.findings import build_failure_finding
from swarmqa.build.settings import BuildConfigError, BuildSettings, PlatformBuild

__all__ = [
    "BuildConfigError",
    "BuildFailed",
    "BuildFailure",
    "BuildReport",
    "BuildResult",
    "BuildSettings",
    "Checkout",
    "PlatformBuild",
    "RepoCache",
    "ShaBuilder",
    "build_failure_finding",
    "repo_name_for",
    "verify_builder",
]


def __getattr__(name: str):
    # Imported lazily: the adapter pulls in swarmqa.verify.
    if name == "verify_builder":
        from swarmqa.build.verify_adapter import verify_builder

        return verify_builder
    raise AttributeError(name)
