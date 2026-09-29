"""DevicePool of macOS Tart VMs.

Each lease takes one of the host-wide Tart slots (at most two, shared with
`TartBackend`), clones `image` to `<prefix>-<slot>` when that VM does not
exist, starts it headless with the build's folder shared in, and waits for
`tart ip` and `tart exec <vm> true`. The device's `address` is the guest IP.
Release stops the VM; with `erase=True` it also deletes the clone so the
next lease starts from a fresh copy-on-write clone of `image`. See
docs/devices.md for the exact commands.
"""

from __future__ import annotations

import json

import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from swarmqa.backends.tart import GUEST_SHARE, TART_SLOT_PREFIX, VMNotReady, _vm_names, wait_for_vm
from swarmqa.devices.capacity import CapacityPolicy, HostResources, host_resources, vm_capacity
from swarmqa.devices.commands import CommandResult, Runner, default_runner, run
from swarmqa.devices.locks import HostLock, try_lock
from swarmqa.devices.protocol import Device, DeviceSetupError, DeviceUnavailable
from swarmqa.models import BuildArtifact, TargetPlatform

BUILD_SHARE = "aqa-build"
_GUEST_SHARES = GUEST_SHARE.rsplit("/", 1)[0]


@dataclass
class _Lease:
    device: Device
    slot_lock: HostLock


class TartVMPool:
    """Lease Tart macOS VMs as `Device(kind="vm", address=<ip>)`."""

    def __init__(
        self,
        *,
        image: str,
        runner: Runner | None = None,
        tart_bin: str = "tart",
        capacity: int | None = None,
        policy: CapacityPolicy | None = None,
        resources: HostResources | None = None,
        lock_root: str | Path | None = None,
        work_root: str | Path | None = None,
        name_prefix: str = "aqa-vm",
        boot_timeout_s: float = 300.0,
        poll_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not image:
            raise ValueError("TartVMPool needs an image to clone")
        self.image = image
        self.runner = runner if runner is not None else default_runner
        self.tart_bin = tart_bin
        self.lock_root = lock_root
        self.work_root = Path(work_root) if work_root is not None else None
        self.name_prefix = name_prefix
        self.boot_timeout_s = boot_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self._capacity = capacity
        self._policy = policy
        self._resources = resources
        self._mutex = threading.Lock()
        self._leases: dict[str, _Lease] = {}
        self._created: set[str] = set()
        # VMs whose erase failed: never reused as-is, deleted again before the next clone.
        self._dirty: set[str] = set()
        # Slots whose VM would not stop. Held for this process's life so the
        # two-VM cap still counts the VM that may be running.
        self._stuck_slots: list[HostLock] = []

    # DevicePool

    def capacity(self, platform: TargetPlatform) -> int:
        if platform != "macos":
            return 0
        if self._capacity is None:
            resources = self._resources or host_resources(self.runner)
            self._capacity = vm_capacity(resources, self._policy)
        return max(0, min(self._capacity, 2))

    def acquire(
        self,
        platform: TargetPlatform,
        build: BuildArtifact | None,
        *,
        timeout_s: float = 600.0,
    ) -> Device:
        count = self.capacity(platform)
        if count <= 0:
            raise DeviceUnavailable(f"TartVMPool has no {platform} devices")
        deadline = self._clock() + max(0.0, timeout_s)
        while True:
            held = self._try_slot(count)
            if held is not None:
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeviceUnavailable(
                    f"no Tart VM slot free after {timeout_s:g}s ({count} on this host)"
                )
            self._sleep(min(self.poll_s, remaining))
        slot, slot_lock = held
        name = f"{self.name_prefix}-{slot}"
        try:
            ip, meta = self._start(name, build)
        except Exception:
            self._stop(name)
            slot_lock.release()
            raise
        device = Device(
            id=name,
            platform="macos",
            kind="vm",
            name=name,
            address=ip,
            work_dir=self._work_dir(name),
            build=build,
            meta={"slot": str(slot), "image": self.image, **meta},
        )
        with self._mutex:
            self._leases[name] = _Lease(device, slot_lock)
        return device

    def release(self, device: Device, *, erase: bool = True) -> None:
        with self._mutex:
            held = self._leases.pop(device.id, None)
        if held is None:
            return
        stopped = False
        try:
            stopped = self._stop(device.id).ok or not self._is_running(device.id)
            if erase:
                deleted = self._tart("delete", device.id)
                with self._mutex:
                    if deleted.ok:
                        self._created.discard(device.id)
                        self._dirty.discard(device.id)
                    else:
                        self._dirty.add(device.id)
        finally:
            if stopped:
                held.slot_lock.release()
            else:
                with self._mutex:
                    self._stuck_slots.append(held.slot_lock)

    def close(self) -> None:
        with self._mutex:
            leased = [held.device for held in self._leases.values()]
        for device in leased:
            self.release(device, erase=False)
        with self._mutex:
            created = sorted(self._created)
            self._created.clear()
        for name in created:
            self._tart("delete", name)

    # Lifecycle

    def _try_slot(self, count: int) -> tuple[int, HostLock] | None:
        for slot in range(count):
            lock = try_lock(f"{TART_SLOT_PREFIX}-{slot}", root=self.lock_root)
            if lock is not None:
                return slot, lock
        return None

    def _start(self, name: str, build: BuildArtifact | None) -> tuple[str, dict[str, str]]:
        listed = self._tart("list", "--format", "json")
        if not listed.ok:
            raise DeviceUnavailable(f"tart list failed: {listed.detail()}")
        exists = name in _vm_names(listed.stdout)
        if exists and name in self._dirty:
            deleted = self._tart("delete", name)
            if not deleted.ok:
                raise DeviceSetupError(f"could not delete stale VM {name}: {deleted.detail()}")
            with self._mutex:
                self._dirty.discard(name)
            exists = False
        if not exists:
            cloned = self._tart("clone", self.image, name, timeout=self.boot_timeout_s)
            if not cloned.ok:
                raise DeviceSetupError(f"could not clone {self.image} to {name}: {cloned.detail()}")
            with self._mutex:
                self._created.add(name)
        args = ["run", "--no-graphics"]
        meta: dict[str, str] = {}
        if build is not None:
            app = Path(build.app_path)
            args.append(f"--dir={BUILD_SHARE}:{app.parent}")
            meta["guest_app_path"] = f"{_GUEST_SHARES}/{BUILD_SHARE}/{app.name}"
        args.append(name)
        started = self._tart(*args, background=True)
        if not started.ok:
            raise DeviceSetupError(f"tart run {name} failed: {started.detail()}")
        try:
            ip = wait_for_vm(
                self.runner,
                self.tart_bin,
                name,
                timeout_s=self.boot_timeout_s,
                poll_s=self.poll_s,
                sleep=self._sleep,
                clock=self._clock,
            )
        except VMNotReady as exc:
            raise DeviceSetupError(str(exc)) from exc
        return ip, meta

    def _stop(self, name: str) -> CommandResult:
        return self._tart("stop", name)

    def _is_running(self, name: str) -> bool:
        """True unless `tart list` says the VM is not running. Unknown counts as running."""
        listed = self._tart("list", "--format", "json")
        if not listed.ok:
            return True
        return _vm_running(listed.stdout, name)

    def _work_dir(self, name: str) -> str:
        if self.work_root is not None:
            self.work_root.mkdir(parents=True, exist_ok=True)
            return tempfile.mkdtemp(prefix=f"{name}-", dir=self.work_root)
        return tempfile.mkdtemp(prefix=f"aqa-{name}-")

    def _tart(self, *args: str, **kwargs: Any) -> CommandResult:
        return run(self.runner, [self.tart_bin, *args], **kwargs)


def _vm_running(stdout: str, name: str) -> bool:
    """Whether `tart list --format json` shows `name` running.

    A missing VM is not running. A VM listed without a readable state counts
    as running, so the VM cap errs on the safe side.
    """
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return True
    entries = payload if isinstance(payload, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("Name") or entry.get("name") or "") != name:
            continue
        state = entry.get("State", entry.get("state"))
        if isinstance(state, str):
            return state.lower() == "running"
        running = entry.get("Running", entry.get("running"))
        return running if isinstance(running, bool) else True
    return False
