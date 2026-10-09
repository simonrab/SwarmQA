"""Check out a repo at a SHA without touching anyone's working copy.

Layout (all under `BuildSettings`, default `~/.aqa`):

- `repos/<name>.git`: a mirror clone of the source (URL or local path),
  fetched when a SHA is missing.
- `checkouts/<name>/<sha>`: one detached `git worktree` of that mirror per
  full SHA, reused when it already exists.

Locks (`swarmqa.devices.locks`, host-wide):

- `build-repo-<name>` guards the mirror: clone, fetch, `worktree add`,
  `worktree remove` and prune. It is held only for those git calls.
- `build-sha-<name>-<sha>` is held by whoever is using that checkout
  (the builder holds it for the whole build). `prune` skips checkouts whose
  lock is taken, so it never deletes a tree under a running build.

Lock order is always sha, then repo; prune takes repo and only *tries* sha
locks, so the two cannot deadlock.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from swarmqa.build.errors import BuildFailed, BuildFailure
from swarmqa.build.runner import Runner, build_runner, run
from swarmqa.build.settings import BuildSettings
from swarmqa.devices.locks import HostLock, LockTimeout

_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


@dataclass
class Checkout:
    repo_name: str
    sha: str
    path: Path
    reused: bool


def repo_name_for(source: str) -> str:
    """`git@github.com:acme/App.git` -> `App`; `/src/app/` -> `app`."""
    text = source.rstrip("/").rstrip("\\")
    base = re.split(r"[/:\\]", text)[-1] if text else ""
    base = base.removesuffix(".git")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")
    return cleaned or "repo"


def is_local_source(source: str) -> bool:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", source) or re.match(r"^[^/]+@[^/:]+:", source):
        return False
    return Path(source).expanduser().exists()


class RepoCache:
    """Mirror clone plus per-SHA worktrees for one source repo."""

    def __init__(self, source: str, settings: BuildSettings, *, runner: Runner | None = None,
                 name: str | None = None):
        self.source = str(Path(source).expanduser().resolve()) if is_local_source(source) else source
        self.settings = settings
        self.runner = runner or build_runner
        self.name = name or settings.repo_name or repo_name_for(source)
        self.mirror = settings.repos_dir() / f"{self.name}.git"
        self.checkouts = settings.checkouts_dir() / self.name

    # Locks ------------------------------------------------------------------

    def repo_lock(self) -> HostLock:
        return HostLock(f"build-repo-{self.name}", root=self.settings.lock_root)

    def sha_lock(self, sha: str) -> HostLock:
        return HostLock(f"build-sha-{self.name}-{sha}", root=self.settings.lock_root)

    def _acquire(self, lock: HostLock, what: str) -> None:
        try:
            lock.acquire(self.settings.lock_timeout_s, poll_s=0.5)
        except LockTimeout as exc:
            raise self._fail("", f"timed out waiting for {what}: {exc}") from exc

    # Git --------------------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None, timeout: float | None = None):
        return run(self.runner, ["git", *args], cwd=str(cwd) if cwd else None, env=dict(_GIT_ENV),
                   timeout=timeout or self.settings.git_timeout_s)

    def mgit(self, *args: str, timeout: float | None = None):
        return self.git(f"--git-dir={self.mirror}", *args, timeout=timeout)

    def _fail(self, sha: str, message: str) -> BuildFailed:
        return BuildFailed(BuildFailure(self.name, sha, "", "checkout", message))

    # Public API -------------------------------------------------------------

    def resolve(self, ref: str) -> str:
        """Full SHA for `ref` (a SHA, short SHA, branch or tag), fetching when missing.

        Call with the repo lock held.
        """
        self._ensure_mirror()
        sha = self._rev_parse(ref)
        if sha:
            return sha
        self._fetch()
        sha = self._rev_parse(ref)
        if sha:
            return sha
        if re.fullmatch(r"[0-9a-f]{7,64}", ref):
            # Hosts like GitHub serve any reachable commit by id (PR heads included).
            fetched = self.mgit("fetch", "--quiet", "origin", ref)
            sha = self._rev_parse(ref) if fetched.ok else None
            if sha:
                return sha
        raise self._fail(ref, f"{ref} not found in {self.source}")

    def checkout(self, ref: str, *, hold: bool = False) -> tuple[Checkout, HostLock | None]:
        """Check out `ref` into `checkouts/<name>/<sha>` and return it.

        With `hold`, the SHA lock is returned still held; release it when the
        checkout is no longer in use. Otherwise it is released on return.
        """
        full = ref if _SHA.match(ref) else None
        if full is None:
            with_lock = self.repo_lock()
            self._acquire(with_lock, f"repo cache {self.name}")
            try:
                full = self.resolve(ref)
            finally:
                with_lock.release()
        sha_lock = self.sha_lock(full)
        self._acquire(sha_lock, f"checkout {self.name}@{full[:12]}")
        try:
            result = self._checkout_locked(full)
        except BaseException:
            sha_lock.release()
            raise
        if not hold:
            sha_lock.release()
            return result, None
        return result, sha_lock

    def _checkout_locked(self, sha: str) -> Checkout:
        path = self.checkouts / sha
        if self._valid_checkout(path, sha):
            _touch(path)
            return Checkout(self.name, sha, path, reused=True)
        lock = self.repo_lock()
        self._acquire(lock, f"repo cache {self.name}")
        try:
            resolved = self.resolve(sha)
            if resolved != sha:
                raise self._fail(sha, f"{sha} resolved to {resolved}")
            if path.exists() or path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            self.mgit("worktree", "prune")
            path.parent.mkdir(parents=True, exist_ok=True)
            added = self.mgit("worktree", "add", "--detach", "--force", str(path), sha)
            if not added.ok:
                raise self._fail(sha, f"git worktree add failed: {added.detail()}")
        finally:
            lock.release()
        if self.settings.submodules and (path / ".gitmodules").is_file():
            sub = self.git("submodule", "update", "--init", "--recursive", cwd=path)
            if not sub.ok:
                raise self._fail(sha, f"git submodule update failed: {sub.detail()}")
        _touch(path)
        return Checkout(self.name, sha, path, reused=False)

    def prune(self, *, keep: int | None = None, max_age_s: float | None = None,
              now: float | None = None) -> list[Path]:
        """Remove old checkouts: all but the `keep` most recently used, and any
        unused for longer than `max_age_s`. Checkouts in use are skipped.
        Returns the removed paths. Build artifacts for removed SHAs go too.
        """
        keep = self.settings.keep if keep is None else keep
        now = time.time() if now is None else now
        if not self.checkouts.is_dir():
            return []
        entries = sorted(
            (path for path in self.checkouts.iterdir() if path.is_dir() and _SHA.match(path.name)),
            key=_last_used, reverse=True,
        )
        doomed = [path for index, path in enumerate(entries)
                  if index >= keep or (max_age_s is not None and now - _last_used(path) > max_age_s)]
        removed: list[Path] = []
        if not doomed:
            return removed
        lock = self.repo_lock()
        self._acquire(lock, f"repo cache {self.name}")
        try:
            for path in doomed:
                busy = self.sha_lock(path.name)
                if not busy.try_acquire():
                    continue
                try:
                    if self.mirror.exists():
                        self.mgit("worktree", "remove", "--force", str(path))
                    shutil.rmtree(path, ignore_errors=True)
                    shutil.rmtree(self.settings.artifacts_dir() / self.name / path.name, ignore_errors=True)
                    removed.append(path)
                finally:
                    busy.release()
            if self.mirror.exists():
                self.mgit("worktree", "prune")
        finally:
            lock.release()
        return removed

    # Internals ----------------------------------------------------------------

    def _ensure_mirror(self) -> None:
        if (self.mirror / "HEAD").is_file():
            return
        if self.mirror.exists():
            shutil.rmtree(self.mirror, ignore_errors=True)
        self.mirror.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--mirror", "--quiet"]
        if is_local_source(self.source):
            args.append("--no-hardlinks")
        cloned = self.git(*args, self.source, str(self.mirror))
        if not cloned.ok:
            shutil.rmtree(self.mirror, ignore_errors=True)
            raise self._fail("", f"git clone of {self.source} failed: {cloned.detail()}")

    def _fetch(self) -> None:
        self.mgit("remote", "set-url", "origin", self.source)
        fetched = self.mgit("fetch", "--prune", "--quiet", "origin")
        if not fetched.ok:
            raise self._fail("", f"git fetch from {self.source} failed: {fetched.detail()}")

    def _rev_parse(self, ref: str) -> str | None:
        result = self.mgit("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        sha = result.stdout.strip() if result.ok else ""
        return sha if _SHA.match(sha) else None

    def _valid_checkout(self, path: Path, sha: str) -> bool:
        if not (path / ".git").is_file():
            return False
        head = self.git("rev-parse", "HEAD", cwd=path)
        return head.ok and head.stdout.strip() == sha


def _touch(path: Path) -> None:
    stamp = path / ".git" if (path / ".git").is_file() else path
    try:
        os.utime(stamp, None)
    except OSError:
        pass


def _last_used(path: Path) -> float:
    stamp = path / ".git"
    try:
        return (stamp if stamp.exists() else path).stat().st_mtime
    except OSError:
        return 0.0
