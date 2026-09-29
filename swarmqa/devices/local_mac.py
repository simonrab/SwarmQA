"""DevicePool of exactly one device: the local Mac, leased host-wide.

macOS apps run in place from `build.app_path`, so there is nothing to
install. The local Mac is the user's own machine, so `erase` only resets app
state when `reset_app_state=True`, and then only with
`defaults delete <bundle_id>`.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from swarmqa.devices.commands import Runner, default_runner, run
from swarmqa.devices.locks import HostLock, LockTimeout
from swarmqa.devices.protocol import Device, DeviceUnavailable
from swarmqa.models import BuildArtifact, TargetPlatform

HOST_LOCK = "local-mac"


class LocalMacPool:
    """Lease the local Mac as `Device(kind="host", id="host")`."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        platform: str | None = None,
        lock_root: str | Path | None = None,
        work_root: str | Path | None = None,
        reset_app_state: bool = False,
        poll_s: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.runner = runner if runner is not None else default_runner
        self.platform = sys.platform if platform is None else platform
        self.lock_root = lock_root
        self.work_root = Path(work_root) if work_root is not None else None
        self.reset_app_state = reset_app_state
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self._mutex = threading.Lock()
        self._lock: HostLock | None = None
        self._device: Device | None = None

    def capacity(self, platform: TargetPlatform) -> int:
        return 1 if platform == "macos" and self.platform == "darwin" else 0

    def acquire(
        self,
        platform: TargetPlatform,
        build: BuildArtifact | None,
        *,
        timeout_s: float = 600.0,
    ) -> Device:
        if self.capacity(platform) <= 0:
            raise DeviceUnavailable(f"the local Mac cannot serve {platform} on {self.platform}")
        lock = HostLock(HOST_LOCK, root=self.lock_root)
        try:
            lock.acquire(timeout_s, poll_s=self.poll_s, sleep=self._sleep, clock=self._clock)
        except LockTimeout as exc:
            raise DeviceUnavailable(f"the local Mac is still leased after {timeout_s:g}s") from exc
        device = Device(
            id="host",
            platform="macos",
            kind="host",
            name=socket.gethostname(),
            work_dir=self._work_dir(),
            build=build,
        )
        with self._mutex:
            self._lock = lock
            self._device = device
        return device

    def release(self, device: Device, *, erase: bool = True) -> None:
        with self._mutex:
            if self._device is None or self._device.id != device.id:
                return
            lock, self._lock = self._lock, None
            self._device = None
        try:
            if erase and self.reset_app_state and device.build is not None:
                run(self.runner, ["defaults", "delete", device.build.bundle_id], timeout=30)
        finally:
            if lock is not None:
                lock.release()

    def close(self) -> None:
        with self._mutex:
            device = self._device
        if device is not None:
            self.release(device)

    def _work_dir(self) -> str:
        if self.work_root is not None:
            self.work_root.mkdir(parents=True, exist_ok=True)
            return tempfile.mkdtemp(prefix="host-", dir=self.work_root)
        return tempfile.mkdtemp(prefix="aqa-host-")
