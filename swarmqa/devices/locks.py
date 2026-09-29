"""Host-wide exclusive leases.

Each lease is an `fcntl.flock` on a file under `~/.aqa/locks` (or
`$AQA_LOCK_DIR`, or an explicit `root`). The kernel drops the lock when the
holder's file descriptor closes, so a crashed process never leaves a stale
lease behind. Lock files are left on disk; deleting them would race with a
waiter that already opened the old inode.

`flock` locks belong to one open file description, so two `HostLock`s for
the same name exclude each other even inside one process.
"""

from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path
from typing import Callable

LOCK_DIR_ENV = "AQA_LOCK_DIR"
DEFAULT_LOCK_DIR = "~/.aqa/locks"


class LockTimeout(Exception):
    """A lease could not be taken before the deadline."""


def lock_root(root: str | Path | None = None) -> Path:
    """Resolve the lock directory: `root`, then `$AQA_LOCK_DIR`, then `~/.aqa/locks`."""
    if root is not None:
        path = Path(root)
    else:
        path = Path(os.environ.get(LOCK_DIR_ENV) or DEFAULT_LOCK_DIR)
    return path.expanduser()


def lock_name(*parts: str) -> str:
    """Join parts into a file-safe lock name."""
    raw = "-".join(part for part in parts if part)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    return (cleaned or "lock")[:120]


class HostLock:
    """One named, host-wide exclusive lease."""

    def __init__(self, name: str, *, root: str | Path | None = None):
        self.name = lock_name(name)
        self.path = lock_root(root) / f"{self.name}.lock"
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lease if it is free. Never blocks."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        self._write_owner()
        return True

    def acquire(
        self,
        timeout_s: float,
        *,
        poll_s: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wait up to `timeout_s` for the lease, else raise `LockTimeout`."""
        deadline = clock() + max(0.0, timeout_s)
        while not self.try_acquire():
            remaining = deadline - clock()
            if remaining <= 0:
                raise LockTimeout(f"lock {self.name} still held after {timeout_s:g}s")
            sleep(min(poll_s, remaining))

    def release(self) -> None:
        """Drop the lease. Safe to call twice."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __del__(self) -> None:
        # A lease nobody references any more is dropped, as on process exit.
        try:
            self.release()
        except Exception:
            pass

    def __enter__(self) -> HostLock:
        if not self.try_acquire():
            raise LockTimeout(f"lock {self.name} is held")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _write_owner(self) -> None:
        # Informational only: the flock, not this text, is the lease.
        assert self._fd is not None
        try:
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass


def try_lock(name: str, *, root: str | Path | None = None) -> HostLock | None:
    """Return a held lock, or None when another holder has it."""
    lock = HostLock(name, root=root)
    return lock if lock.try_acquire() else None


def try_slot(prefix: str, count: int, *, root: str | Path | None = None) -> tuple[int, HostLock] | None:
    """Take the first free lease among `<prefix>-0` .. `<prefix>-<count-1>`."""
    for index in range(max(0, count)):
        lock = try_lock(f"{prefix}-{index}", root=root)
        if lock is not None:
            return index, lock
    return None


def acquire_slot(
    prefix: str,
    count: int,
    timeout_s: float,
    *,
    root: str | Path | None = None,
    poll_s: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, HostLock]:
    """Wait up to `timeout_s` for one of `count` slots, else raise `LockTimeout`."""
    deadline = clock() + max(0.0, timeout_s)
    while True:
        got = try_slot(prefix, count, root=root)
        if got is not None:
            return got
        remaining = deadline - clock()
        if remaining <= 0:
            raise LockTimeout(f"all {count} {prefix} slots still held after {timeout_s:g}s")
        sleep(min(poll_s, remaining))


def simulator_lock_name(udid_or_name: str) -> str:
    """Host-wide lock key for one simulator, shared by the iOS pool and driver."""
    return lock_name("simulator", udid_or_name)
