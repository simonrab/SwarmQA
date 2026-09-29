"""DevicePool of iOS simulators cloned from a golden device.

Each capacity slot owns one clone named `<prefix>-<golden>-<slot>`. A lease
takes the slot's host-wide lock and a lock on the clone's UDID
(`simulator-<udid>`, the same key the iOS driver uses), boots the clone
with `simctl bootstatus -b`, and installs the build. Release erases the
clone (or uninstalls the app) and keeps it for the next lease; `close`
deletes the clones this pool created. See docs/devices.md for the exact
commands.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from swarmqa.devices.capacity import (
    CapacityPolicy,
    HostResources,
    host_resources,
    simulator_capacity,
)
from swarmqa.devices.commands import CommandResult, Runner, default_runner, run
from swarmqa.devices.locks import HostLock, simulator_lock_name, try_lock
from swarmqa.devices.protocol import Device, DeviceSetupError, DeviceUnavailable
from swarmqa.models import BuildArtifact, TargetPlatform

SLOT_PREFIX = "ios-sim-slot"
EraseMode = Literal["erase", "uninstall"]


@dataclass
class _Lease:
    device: Device
    slot_lock: HostLock
    udid_lock: HostLock


class IOSSimulatorPool:
    """Lease cloned iOS simulators, one per worker.

    Give `golden` (a name or UDID of a shut-down simulator to `simctl clone`),
    or `device_type` plus optional `runtime` for `simctl create`.
    `erase_mode="erase"` shuts the clone down and runs `simctl erase` on
    release; `"uninstall"` only removes the app and leaves it booted.
    """

    def __init__(
        self,
        *,
        golden: str | None = None,
        device_type: str | None = None,
        runtime: str | None = None,
        runner: Runner | None = None,
        capacity: int | None = None,
        policy: CapacityPolicy | None = None,
        resources: HostResources | None = None,
        lock_root: str | Path | None = None,
        work_root: str | Path | None = None,
        name_prefix: str = "aqa-sim",
        erase_mode: EraseMode = "erase",
        boot_timeout_s: float = 300.0,
        poll_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not golden and not device_type:
            raise ValueError("IOSSimulatorPool needs golden or device_type")
        self.golden = golden
        self.device_type = device_type
        self.runtime = runtime
        self.runner = runner if runner is not None else default_runner
        self.lock_root = lock_root
        self.work_root = Path(work_root) if work_root is not None else None
        self.name_prefix = name_prefix
        self.erase_mode = erase_mode
        self.boot_timeout_s = boot_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self._capacity = capacity
        self._policy = policy
        self._resources = resources
        self._mutex = threading.Lock()
        self._leases: dict[str, _Lease] = {}
        self._clones: dict[str, str] = {}  # clone name -> udid
        self._created: set[str] = set()

    # DevicePool

    def capacity(self, platform: TargetPlatform) -> int:
        if platform != "ios":
            return 0
        if self._capacity is None:
            resources = self._resources or host_resources(self.runner)
            self._capacity = simulator_capacity(resources, self._policy)
        return self._capacity

    def acquire(
        self,
        platform: TargetPlatform,
        build: BuildArtifact | None,
        *,
        timeout_s: float = 600.0,
    ) -> Device:
        count = self.capacity(platform)
        if count <= 0:
            raise DeviceUnavailable(f"IOSSimulatorPool has no {platform} devices")
        deadline = self._clock() + max(0.0, timeout_s)
        while True:
            held = self._try_claim(count)
            if held is not None:
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeviceUnavailable(
                    f"no iOS simulator free after {timeout_s:g}s ({count} on this host)"
                )
            self._sleep(min(self.poll_s, remaining))
        slot, name, udid, slot_lock, udid_lock = held
        try:
            self._boot(udid)
            if build is not None:
                self._install(udid, build)
        except Exception:
            udid_lock.release()
            slot_lock.release()
            raise
        device = Device(
            id=udid,
            platform="ios",
            kind="simulator",
            name=name,
            work_dir=self._work_dir(udid),
            build=build,
            meta={"slot": str(slot), "source": self._source_key()},
        )
        with self._mutex:
            self._leases[udid] = _Lease(device, slot_lock, udid_lock)
        return device

    def release(self, device: Device, *, erase: bool = True) -> None:
        with self._mutex:
            held = self._leases.pop(device.id, None)
        if held is None:
            return
        try:
            if erase:
                self._wipe(device)
        finally:
            held.udid_lock.release()
            held.slot_lock.release()

    def close(self) -> None:
        with self._mutex:
            leased = [held.device for held in self._leases.values()]
        for device in leased:
            self.release(device)
        with self._mutex:
            created = sorted(self._created)
            self._created.clear()
            self._clones = {name: udid for name, udid in self._clones.items() if udid not in created}
        for udid in created:
            self._shutdown(udid)
            self._simctl("delete", udid)

    # Claiming

    def _try_claim(self, count: int) -> tuple[int, str, str, HostLock, HostLock] | None:
        for slot in range(count):
            slot_lock = try_lock(f"{SLOT_PREFIX}-{slot}", root=self.lock_root)
            if slot_lock is None:
                continue
            try:
                name = self._clone_name(slot)
                udid = self._ensure_clone(name)
            except Exception:
                slot_lock.release()
                raise
            udid_lock = try_lock(simulator_lock_name(udid), root=self.lock_root)
            if udid_lock is None:
                slot_lock.release()
                continue
            return slot, name, udid, slot_lock, udid_lock
        return None

    def _source_key(self) -> str:
        if self.golden:
            return self.golden
        return "-".join(part for part in (self.device_type, self.runtime) if part)

    def _clone_name(self, slot: int) -> str:
        parts = [self.golden] if self.golden else [self.device_type, self.runtime]
        short = "-".join(str(part).split(".")[-1] for part in parts if part)
        source = re.sub(r"[^A-Za-z0-9]+", "-", short).strip("-")
        return f"{self.name_prefix}-{source or 'device'}-{slot}"

    def _ensure_clone(self, name: str) -> str:
        with self._mutex:
            known = self._clones.get(name)
        if known:
            return known
        udid = self._find_existing(name)
        if udid is None:
            if self.golden:
                result = self._simctl("clone", self.golden, name, timeout=self.boot_timeout_s)
            else:
                args = ["create", name, str(self.device_type)]
                if self.runtime:
                    args.append(self.runtime)
                result = self._simctl(*args, timeout=self.boot_timeout_s)
            if not result.ok:
                raise DeviceSetupError(f"could not create simulator {name}: {result.detail()}")
            udid = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
            if not udid:
                raise DeviceSetupError(f"simctl did not report a UDID for {name}")
            with self._mutex:
                self._created.add(udid)
        with self._mutex:
            self._clones[name] = udid
        return udid

    def _find_existing(self, name: str) -> str | None:
        result = self._simctl("list", "devices", "-j")
        if not result.ok:
            raise DeviceUnavailable(f"simctl list failed: {result.detail()}")
        try:
            document = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise DeviceUnavailable("simctl list did not return JSON") from exc
        devices = document.get("devices") if isinstance(document, dict) else None
        if not isinstance(devices, dict):
            return None
        for entries in devices.values():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("isAvailable") is False:
                    continue
                if str(entry.get("name") or "") == name and entry.get("udid"):
                    return str(entry["udid"])
        return None

    # Device lifecycle

    def _boot(self, udid: str) -> None:
        result = self._simctl("bootstatus", udid, "-b", timeout=self.boot_timeout_s)
        if not result.ok:
            raise DeviceSetupError(
                f"simulator {udid} did not boot within {self.boot_timeout_s:g}s: {result.detail()}"
            )

    def _install(self, udid: str, build: BuildArtifact) -> None:
        result = self._simctl("install", udid, build.app_path, timeout=self.boot_timeout_s)
        if not result.ok:
            raise DeviceSetupError(f"could not install {build.app_path} on {udid}: {result.detail()}")

    def _wipe(self, device: Device) -> None:
        if self.erase_mode == "uninstall":
            if device.build is not None:
                self._simctl("uninstall", device.id, device.build.bundle_id)
            return
        # `simctl erase` refuses a booted device.
        self._shutdown(device.id)
        result = self._simctl("erase", device.id, timeout=self.boot_timeout_s)
        if not result.ok:
            # A clone that will not erase is not reused.
            with self._mutex:
                self._clones = {n: u for n, u in self._clones.items() if u != device.id}
                created = device.id in self._created
                self._created.discard(device.id)
            if created:
                self._simctl("delete", device.id)

    def _shutdown(self, udid: str) -> CommandResult:
        result = self._simctl("shutdown", udid)
        if not result.ok and "current state: shutdown" in result.detail().lower():
            return CommandResult(result.args, 0, result.stdout, result.stderr)
        return result

    def _work_dir(self, udid: str) -> str:
        if self.work_root is not None:
            self.work_root.mkdir(parents=True, exist_ok=True)
            return tempfile.mkdtemp(prefix=f"{udid}-", dir=self.work_root)
        return tempfile.mkdtemp(prefix=f"aqa-{udid}-")

    def _simctl(self, *args: str, **kwargs: Any) -> CommandResult:
        return run(self.runner, ["xcrun", "simctl", *args], **kwargs)
