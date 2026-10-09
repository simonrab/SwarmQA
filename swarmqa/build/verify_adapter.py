"""Adapter from the per-SHA builder to `swarmqa.verify.build.Builder`.

`core.verify(..., builder=verify_builder())` builds the committed `ref`
(default `HEAD`) of the app's repo in a clean checkout, instead of running
`app.build_command` in the working copy. On success it points
`config.app.path` and `config.app.bundle_id` at the new build, because the
verify run plans its devices from `config.app` after the build step.

The repo is `repo`, else `build.repo`, else the git top level of
`app.source_dir`. With a local repo whose `app.source_dir` is a subdirectory,
that subdirectory becomes `build.project` unless one is set.

A local repo with uncommitted changes is an error unless `allow_dirty`:
the clean checkout would not contain them, so the verdict would be about
the wrong code. With no repo at all, the adapter falls back to
`swarmqa.verify.build.build_app`.
"""

from __future__ import annotations

import copy
import os
import shutil
import time
from pathlib import Path

from swarmqa.build.builder import ShaBuilder
from swarmqa.build.checkout import is_local_source
from swarmqa.build.runner import Runner, build_runner, run
from swarmqa.build.settings import BuildSettings
from swarmqa.models import CampaignConfig
from swarmqa.verify.build import Builder, BuildOutcome, build_app

MAX_DIRTY_PATHS = 5


def verify_builder(
    settings: BuildSettings | None = None,
    *,
    repo: str | None = None,
    ref: str = "HEAD",
    platform: str | None = None,
    runner: Runner | None = None,
    force: bool = False,
    allow_dirty: bool = False,
) -> Builder:
    """Return a `Builder` for `core.verify` backed by `ShaBuilder`."""
    runner = runner or build_runner

    def build(config: CampaignConfig, log_path: Path) -> BuildOutcome:
        started = time.monotonic()
        try:
            options = copy.deepcopy(settings) if settings is not None else BuildSettings.from_config(config)
        except Exception as exc:  # noqa: BLE001 - a bad [build] table is a build error
            return BuildOutcome(ok=False, message=f"build settings: {exc}")
        source = repo or options.repo
        source_dir = config.app.source_dir
        if not source and not source_dir:
            return build_app(config, log_path)
        source = source or source_dir
        target = ref
        notes: list[str] = []
        if is_local_source(str(source)):
            top = (_git(runner, source, "rev-parse", "--show-toplevel") or "").strip()
            if not top:
                return BuildOutcome(ok=False, message=f"{source} is not a git repository")
            if source_dir and options.project is None:
                relative = os.path.relpath(Path(source_dir).expanduser().resolve(), Path(top).resolve())
                if relative != "." and not relative.startswith(".."):
                    options.project = relative
            dirty = _git(runner, top, "status", "--porcelain")
            dirty = (dirty or "").rstrip()
            if dirty and not allow_dirty:
                paths = [line[3:] for line in dirty.splitlines() if line.strip()]
                shown = ", ".join(paths[:MAX_DIRTY_PATHS]) + (" ..." if len(paths) > MAX_DIRTY_PATHS else "")
                return BuildOutcome(ok=False, message=(
                    f"{top} has uncommitted changes ({shown}); the per-SHA build uses committed code only. "
                    "Commit the fix, or verify with --no-build after building it yourself."))
            if dirty:
                notes.append(f"uncommitted changes in {top} were not built")
            resolved = _git(runner, top, "rev-parse", "--verify", f"{ref}^{{commit}}")
            if resolved is None:
                return BuildOutcome(ok=False, message=f"{ref} does not name a commit in {top}")
            target, source = resolved.strip(), top
        builder = ShaBuilder(options, runner=runner)
        result = builder.build_result(target, platform or config.app.platform, repo=str(source), force=force)
        log = _copy_log(result.failure.log_path if result.failure else result.artifact.log_path, Path(log_path))
        duration = time.monotonic() - started
        if result.failure is not None:
            return BuildOutcome(ok=False, message=result.failure.summary(), log=log, duration_s=duration)
        artifact = result.artifact
        assert artifact is not None
        config.app.path = artifact.app_path
        config.app.bundle_id = artifact.bundle_id
        how = "reused the build of" if result.reused else "built"
        message = f"{how} {artifact.sha[:12]} for {artifact.platform}: {artifact.app_path}"
        return BuildOutcome(ok=True, message="\n".join([message, *notes]), log=log, duration_s=duration)

    return build


def _git(runner: Runner, cwd: str | Path, *args: str) -> str | None:
    result = run(runner, ["git", *args], cwd=str(Path(cwd).expanduser()), timeout=60)
    return result.stdout if result.ok else None


def _copy_log(source: str | None, destination: Path) -> str | None:
    if not source or not Path(source).is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
    except OSError:
        return source
    return str(destination)
