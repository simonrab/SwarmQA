"""WP-A4 device pools: host locks, capacity, and the three DevicePools.

Everything runs on Linux against fake `simctl`, `tart`, and `sysctl` output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from swarmqa.backends.tart import TART_SLOT_PREFIX, TartBackend
from swarmqa.devices.capacity import (
    TART_VM_LIMIT,
    CapacityPolicy,
    HostResources,
    host_resources,
    simulator_capacity,
    vm_capacity,
)
from swarmqa.devices.ios_pool import IOSSimulatorPool
from swarmqa.devices.local_mac import LocalMacPool
from swarmqa.devices.locks import (
    LOCK_DIR_ENV,
    HostLock,
    LockTimeout,
    acquire_slot,
    lock_root,
    simulator_lock_name,
    try_lock,
)
from swarmqa.devices.protocol import DevicePool, DeviceSetupError, DeviceUnavailable, lease
from swarmqa.devices.tart_pool import TartVMPool
from swarmqa.models import BuildArtifact
from swarmqa.testing import sample_config, scripted_shard

GB = 1024**3


@pytest.fixture(autouse=True)
def _locks(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "locks"
    monkeypatch.setenv(LOCK_DIR_ENV, str(root))
    return root


# Fake command output


def _ok(args, stdout: str = "") -> CompletedProcess:
    return CompletedProcess(list(args), 0, stdout, "")


def _fail(args, stderr: str, code: int = 1) -> CompletedProcess:
    return CompletedProcess(list(args), code, "", stderr)


class FakeSimctl:
    """Recorded `xcrun simctl` behaviour, including its state rules."""

    def __init__(self, existing: dict[str, str] | None = None, *, boot_fails: bool = False):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.devices: dict[str, dict] = {}
        for udid, name in (existing or {}).items():
            self.devices[udid] = {"name": name, "state": "Shutdown", "apps": set()}
        self.boot_fails = boot_fails
        self._serial = 0
        self._mutex = threading.Lock()

    def __call__(self, args, **kwargs):
        with self._mutex:
            return self._handle(list(args), kwargs)

    def _handle(self, args, kwargs):
        self.calls.append(args)
        self.kwargs.append(kwargs)
        assert args[:2] == ["xcrun", "simctl"], args
        verb, rest = args[2], args[3:]
        if verb == "list":
            entries = [
                {"udid": udid, "name": dev["name"], "state": dev["state"], "isAvailable": True}
                for udid, dev in self.devices.items()
            ]
            return _ok(args, json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": entries}}))
        if verb in {"clone", "create"}:
            self._serial += 1
            udid = f"00000000-0000-0000-0000-{self._serial:012d}"
            self.devices[udid] = {"name": rest[1] if verb == "clone" else rest[0], "state": "Shutdown", "apps": set()}
            return _ok(args, udid + "\n")
        dev = self.devices.get(rest[0]) if rest else None
        if dev is None:
            return _fail(args, "Invalid device")
        if verb == "bootstatus":
            if self.boot_fails:
                return _fail(args, "timed out waiting for boot", 124)
            dev["state"] = "Booted"
            return _ok(args, "Device already booted")
        if verb == "install":
            dev["apps"].add(rest[1])
            return _ok(args)
        if verb == "uninstall":
            dev["apps"] = {app for app in dev["apps"] if rest[1] not in app}
            return _ok(args)
        if verb == "shutdown":
            if dev["state"] == "Shutdown":
                return _fail(args, "Unable to shutdown device in current state: Shutdown", 149)
            dev["state"] = "Shutdown"
            return _ok(args)
        if verb == "erase":
            if dev["state"] != "Shutdown":
                return _fail(args, "Unable to erase contents and settings in current state: Booted", 149)
            dev["apps"] = set()
            return _ok(args)
        if verb == "delete":
            del self.devices[rest[0]]
            return _ok(args)
        return _fail(args, f"unknown verb {verb}")

    def verbs(self, verb: str) -> list[list[str]]:
        return [call for call in self.calls if call[2] == verb]


class FakeTart:
    """Recorded `tart` behaviour: clone, background run, ip, exec, stop, delete."""

    def __init__(
        self,
        existing: list[str] | None = None,
        *,
        agent_never_ready: bool = False,
        stop_fails: bool = False,
        delete_fails: bool = False,
    ):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.vms: set[str] = set(existing or [])
        self.running: set[str] = set()
        self.agent_never_ready = agent_never_ready
        self.stop_fails = stop_fails
        self.delete_fails = delete_fails
        self._mutex = threading.Lock()

    def __call__(self, args, **kwargs):
        with self._mutex:
            args = list(args)
            self.calls.append(args)
            self.kwargs.append(kwargs)
            verb, rest = args[1], args[2:]
            if verb == "list":
                return _ok(args, json.dumps([
                    {"Name": name, "State": "running" if name in self.running else "stopped"}
                    for name in sorted(self.vms)
                ]))
            if verb == "clone":
                self.vms.add(rest[1])
                return _ok(args)
            name = rest[-1] if verb == "run" else rest[0]
            if name not in self.vms:
                return _fail(args, f"the specified VM \"{name}\" does not exist")
            if verb == "run":
                assert kwargs.get("background") is True
                self.running.add(name)
                return _ok(args)
            if verb == "ip":
                if name not in self.running:
                    return _fail(args, "no IP address found")
                return _ok(args, f"192.168.64.{sorted(self.vms).index(name) + 10}\n")
            if verb == "exec":
                if name not in self.running or self.agent_never_ready:
                    return _fail(args, "guest agent is not running")
                return _ok(args)
            if verb == "stop":
                if self.stop_fails:
                    return _fail(args, "could not stop")
                self.running.discard(name)
                return _ok(args)
            if verb == "delete":
                if self.delete_fails:
                    return _fail(args, "could not delete")
                self.vms.discard(name)
                self.running.discard(name)
                return _ok(args)
            return _fail(args, f"unknown verb {verb}")

    def verbs(self, verb: str) -> list[list[str]]:
        return [call for call in self.calls if call[1] == verb]


class FakeHostRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return _ok(args)


IOS_BUILD = BuildArtifact(platform="ios", app_path="/builds/Sample.app", bundle_id="dev.swarmqa.sample")
MAC_BUILD = BuildArtifact(platform="macos", app_path="/builds/mac/Sample.app", bundle_id="dev.swarmqa.sample")


# Locks


def test_lock_root_prefers_argument_then_env(tmp_path: Path, monkeypatch):
    assert lock_root(tmp_path / "x") == tmp_path / "x"
    assert lock_root() == Path(os.environ[LOCK_DIR_ENV])
    monkeypatch.delenv(LOCK_DIR_ENV)
    assert lock_root() == Path("~/.aqa/locks").expanduser()


def test_host_lock_is_exclusive_even_in_one_process(_locks: Path):
    first = HostLock("simulator-A")
    second = HostLock("simulator-A")
    assert first.try_acquire()
    assert first.path.parent == _locks
    assert not second.try_acquire()
    first.release()
    first.release()
    assert second.try_acquire()
    second.release()


def test_blocking_acquire_times_out_and_succeeds_after_release():
    holder = try_lock("slot")
    assert holder is not None
    waiter = HostLock("slot")
    with pytest.raises(LockTimeout):
        waiter.acquire(0.05, poll_s=0.01)
    threading.Timer(0.05, holder.release).start()
    waiter.acquire(5, poll_s=0.01)
    assert waiter.held
    waiter.release()


def test_lock_is_released_when_the_holder_process_dies(_locks: Path):
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from swarmqa.devices.locks import try_lock
        lock = try_lock("crashy", root={str(_locks)!r})
        print("held" if lock else "busy", flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        assert try_lock("crashy") is None
        child.kill()
        child.wait(10)
        lock = try_lock("crashy")
        assert lock is not None
        lock.release()
    finally:
        if child.poll() is None:
            child.kill()


def test_acquire_slot_hands_out_distinct_slots_then_times_out():
    first = acquire_slot("pool", 2, 0)
    second = acquire_slot("pool", 2, 0)
    assert {first[0], second[0]} == {0, 1}
    with pytest.raises(LockTimeout):
        acquire_slot("pool", 2, 0.03, poll_s=0.01)
    first[1].release()
    third = acquire_slot("pool", 2, 0)
    assert third[0] == first[0]
    second[1].release()
    third[1].release()


# Capacity


def test_simulator_capacity_uses_the_tighter_of_ram_and_cores():
    assert simulator_capacity(HostResources(36 * GB, 12)) == 6  # cores bind: 12 // 2
    assert simulator_capacity(HostResources(16 * GB, 12)) == 4  # RAM binds: (16 - 4) // 2.5
    assert simulator_capacity(HostResources(4 * GB, 2)) == 1  # never below one
    policy = CapacityPolicy(gb_per_simulator=3, cores_per_simulator=1, reserve_gb=0, max_simulators=5)
    assert simulator_capacity(HostResources(64 * GB, 16), policy) == 5


def test_vm_capacity_is_capped_at_two():
    assert TART_VM_LIMIT == 2
    assert vm_capacity(HostResources(512 * GB, 64)) == 2
    assert vm_capacity(HostResources(512 * GB, 64), CapacityPolicy(max_vms=9)) == 2
    assert vm_capacity(HostResources(16 * GB, 8)) == 1


def test_host_resources_reads_sysctl_on_macos():
    calls = []

    def runner(args, **kwargs):
        calls.append(list(args))
        return _ok(args, {"hw.memsize": "34359738368\n", "hw.ncpu": "12\n"}[args[-1]])

    resources = host_resources(runner, platform="darwin")
    assert resources == HostResources(32 * GB, 12)
    assert calls == [["sysctl", "-n", "hw.memsize"], ["sysctl", "-n", "hw.ncpu"]]


def test_host_resources_uses_sysconf_elsewhere():
    values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 4 * GB // 4096}
    resources = host_resources(platform="linux", sysconf=values.__getitem__, cpu_count=lambda: 6)
    assert resources == HostResources(4 * GB, 6)


def test_host_resources_falls_back_when_sysctl_fails():
    resources = host_resources(
        lambda args, **kw: _fail(args, "nope"),
        platform="darwin",
        sysconf={"SC_PAGE_SIZE": 1, "SC_PHYS_PAGES": 8 * GB}.__getitem__,
        cpu_count=lambda: 4,
    )
    assert resources == HostResources(8 * GB, 4)


# Shared conformance suite


class Harness:
    def __init__(self, make, platform, build, runner, erase_marker, capacity):
        self.make = make
        self.platform = platform
        self.build = build
        self.runner = runner
        self.erase_marker = erase_marker
        self.capacity = capacity

    def erase_calls(self) -> int:
        return sum(1 for call in self.runner.calls if self.erase_marker(call))


def _ios_harness(tmp_path: Path) -> Harness:
    runner = FakeSimctl()

    def make():
        return IOSSimulatorPool(
            golden="Golden iPhone", runner=runner, capacity=2, work_root=tmp_path / "work", poll_s=0.01
        )

    # The default release wipe is `simctl uninstall`; erase mode is tested below.
    return Harness(make, "ios", IOS_BUILD, runner, lambda call: call[2:3] == ["uninstall"], 2)


def _tart_harness(tmp_path: Path) -> Harness:
    runner = FakeTart()

    def make():
        return TartVMPool(
            image="ghcr.io/cirruslabs/macos-sequoia-base:latest",
            runner=runner,
            capacity=2,
            work_root=tmp_path / "work",
            poll_s=0.01,
        )

    return Harness(make, "macos", MAC_BUILD, runner, lambda call: call[1:2] == ["delete"], 2)


def _local_harness(tmp_path: Path) -> Harness:
    runner = FakeHostRunner()

    def make():
        return LocalMacPool(
            runner=runner, platform="darwin", reset_app_state=True, work_root=tmp_path / "work", poll_s=0.01
        )

    return Harness(make, "macos", MAC_BUILD, runner, lambda call: call[:2] == ["defaults", "delete"], 1)


@pytest.fixture(params=["ios", "tart", "local"])
def harness(request, tmp_path: Path) -> Harness:
    return {"ios": _ios_harness, "tart": _tart_harness, "local": _local_harness}[request.param](tmp_path)


def test_conformance_is_a_device_pool(harness: Harness):
    pool = harness.make()
    assert isinstance(pool, DevicePool)
    assert pool.capacity(harness.platform) == harness.capacity
    other = "ios" if harness.platform == "macos" else "macos"
    assert pool.capacity(other) == 0
    with pytest.raises(DeviceUnavailable):
        pool.acquire(other, None, timeout_s=0.01)


def test_conformance_acquires_up_to_capacity_then_times_out(harness: Harness):
    pool = harness.make()
    devices = [pool.acquire(harness.platform, harness.build, timeout_s=1) for _ in range(harness.capacity)]
    assert len({device.id for device in devices}) == harness.capacity
    for device in devices:
        assert device.platform == harness.platform
        assert device.build is harness.build
        assert Path(device.work_dir).is_dir()
    started = time.monotonic()
    with pytest.raises(DeviceUnavailable):
        pool.acquire(harness.platform, harness.build, timeout_s=0.05)
    assert time.monotonic() - started < 5
    pool.close()


def test_conformance_leases_are_host_wide(harness: Harness):
    first, second = harness.make(), harness.make()
    held = [first.acquire(harness.platform, None, timeout_s=1) for _ in range(harness.capacity)]
    with pytest.raises(DeviceUnavailable):
        second.acquire(harness.platform, None, timeout_s=0.05)
    first.release(held[0])
    device = second.acquire(harness.platform, None, timeout_s=1)
    assert device.id == held[0].id
    second.close()
    first.close()


def test_conformance_release_is_idempotent(harness: Harness):
    pool = harness.make()
    device = pool.acquire(harness.platform, harness.build, timeout_s=1)
    pool.release(device)
    erased = harness.erase_calls()
    pool.release(device)
    pool.release(device, erase=False)
    assert harness.erase_calls() == erased == 1
    again = [pool.acquire(harness.platform, harness.build, timeout_s=1) for _ in range(harness.capacity)]
    assert device.id in {item.id for item in again}
    pool.close()


def test_conformance_lease_releases_on_exception(harness: Harness):
    pool = harness.make()
    with pytest.raises(RuntimeError):
        with lease(pool, harness.platform, harness.build, timeout_s=1):
            raise RuntimeError("worker crashed")
    devices = [pool.acquire(harness.platform, harness.build, timeout_s=1) for _ in range(harness.capacity)]
    assert len(devices) == harness.capacity
    pool.close()


def test_conformance_erase_flag_is_honoured(harness: Harness):
    pool = harness.make()
    device = pool.acquire(harness.platform, harness.build, timeout_s=1)
    pool.release(device, erase=False)
    assert harness.erase_calls() == 0
    device = pool.acquire(harness.platform, harness.build, timeout_s=1)
    pool.release(device, erase=True)
    assert harness.erase_calls() == 1
    pool.close()


def test_conformance_waiter_gets_the_released_device(harness: Harness):
    pool = harness.make()
    held = [pool.acquire(harness.platform, None, timeout_s=1) for _ in range(harness.capacity)]
    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.acquire(harness.platform, None, timeout_s=5)))
    waiter.start()
    time.sleep(0.05)
    pool.release(held[-1])
    waiter.join(5)
    assert got and got[0].id == held[-1].id
    pool.close()


# iOS simulator pool


def test_ios_pool_clones_boots_installs_and_reuses(tmp_path: Path):
    runner = FakeSimctl()
    pool = IOSSimulatorPool(
        golden="Golden iPhone", runner=runner, capacity=2, erase_mode="erase", work_root=tmp_path
    )
    device = pool.acquire("ios", IOS_BUILD)
    assert device.kind == "simulator"
    assert device.address == ""
    assert device.name == "aqa-sim-Golden-iPhone-0"
    udid = device.id
    assert runner.calls == [
        ["xcrun", "simctl", "list", "devices", "-j"],
        ["xcrun", "simctl", "clone", "Golden iPhone", "aqa-sim-Golden-iPhone-0"],
        ["xcrun", "simctl", "bootstatus", udid, "-b"],
        ["xcrun", "simctl", "install", udid, "/builds/Sample.app"],
    ]
    assert runner.kwargs[2]["timeout"] == pool.boot_timeout_s
    pool.release(device)
    # erase needs a shut-down device: shutdown, then erase.
    assert runner.calls[-2:] == [
        ["xcrun", "simctl", "shutdown", udid],
        ["xcrun", "simctl", "erase", udid],
    ]
    again = pool.acquire("ios", IOS_BUILD)
    assert again.id == udid
    assert len(runner.verbs("clone")) == 1
    pool.close()
    assert runner.verbs("delete") == [["xcrun", "simctl", "delete", udid]]
    assert runner.devices == {}


def test_ios_pool_erase_tolerates_an_already_shut_down_clone(tmp_path: Path):
    runner = FakeSimctl()
    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=1, erase_mode="erase", work_root=tmp_path)
    device = pool.acquire("ios", None)
    runner.devices[device.id]["state"] = "Shutdown"
    pool.release(device)
    assert runner.verbs("erase") == [["xcrun", "simctl", "erase", device.id]]
    assert pool.acquire("ios", None, timeout_s=0).id == device.id


def test_ios_pool_uninstall_mode_keeps_the_clone_booted(tmp_path: Path):
    runner = FakeSimctl()
    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=1, work_root=tmp_path)
    assert pool.erase_mode == "uninstall"  # the default
    device = pool.acquire("ios", IOS_BUILD)
    pool.release(device)
    assert runner.verbs("uninstall") == [["xcrun", "simctl", "uninstall", device.id, "dev.swarmqa.sample"]]
    assert runner.verbs("shutdown") == []
    assert runner.devices[device.id]["state"] == "Booted"


def test_ios_pool_creates_from_device_type_and_runtime(tmp_path: Path):
    runner = FakeSimctl()
    pool = IOSSimulatorPool(
        device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-16",
        runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-0",
        runner=runner,
        capacity=1,
        work_root=tmp_path,
    )
    device = pool.acquire("ios", None)
    assert runner.verbs("create") == [
        [
            "xcrun", "simctl", "create", "aqa-sim-iPhone-16-iOS-18-0-0",
            "com.apple.CoreSimulator.SimDeviceType.iPhone-16",
            "com.apple.CoreSimulator.SimRuntime.iOS-18-0",
        ]
    ]
    assert device.name == "aqa-sim-iPhone-16-iOS-18-0-0"


def test_ios_pool_reuses_a_leftover_clone_and_does_not_delete_it(tmp_path: Path):
    leftover = "99999999-0000-0000-0000-000000000000"
    runner = FakeSimctl({leftover: "aqa-sim-G-0"})
    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=1, work_root=tmp_path)
    assert pool.acquire("ios", None).id == leftover
    assert runner.verbs("clone") == []
    pool.close()
    assert runner.verbs("delete") == []
    assert leftover in runner.devices


def test_ios_pool_boot_failure_raises_and_frees_the_slot(tmp_path: Path):
    runner = FakeSimctl(boot_fails=True)
    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=1, boot_timeout_s=5, work_root=tmp_path)
    with pytest.raises(DeviceSetupError, match="did not boot within 5s"):
        pool.acquire("ios", IOS_BUILD)
    assert runner.verbs("install") == []
    runner.boot_fails = False
    assert pool.acquire("ios", IOS_BUILD, timeout_s=0).kind == "simulator"


def test_ios_pool_clone_failure_raises(tmp_path: Path):
    def runner(args, **kwargs):
        if args[2] == "list":
            return _ok(args, json.dumps({"devices": {}}))
        return _fail(args, "Unable to clone device in current state: Booted")

    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=1, work_root=tmp_path)
    with pytest.raises(DeviceUnavailable, match="current state: Booted"):
        pool.acquire("ios", None)
    assert try_lock("ios-sim-slot-0") is not None


def test_ios_pool_skips_a_udid_the_driver_holds(tmp_path: Path):
    runner = FakeSimctl()
    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=2, work_root=tmp_path, poll_s=0.01)
    first = pool.acquire("ios", None)
    pool.release(first, erase=False)
    held = try_lock(simulator_lock_name(first.id))
    second = pool.acquire("ios", None)
    assert second.id != first.id
    with pytest.raises(DeviceUnavailable):
        pool.acquire("ios", None, timeout_s=0.03)
    held.release()


def test_ios_pool_erase_failure_retires_the_clone(tmp_path: Path):
    runner = FakeSimctl()
    pool = IOSSimulatorPool(golden="G", runner=runner, capacity=1, erase_mode="erase", work_root=tmp_path)
    device = pool.acquire("ios", None)
    original = runner._handle

    def broken(args, kwargs):
        if args[2] == "erase":
            runner.calls.append(args)
            return _fail(args, "erase failed")
        return original(args, kwargs)

    runner._handle = broken
    pool.release(device)
    assert runner.verbs("delete") == [["xcrun", "simctl", "delete", device.id]]
    runner._handle = original
    assert pool.acquire("ios", None, timeout_s=0).id != device.id


def test_ios_pool_capacity_comes_from_host_resources():
    pool = IOSSimulatorPool(golden="G", runner=FakeSimctl(), resources=HostResources(36 * GB, 12))
    assert pool.capacity("ios") == 6
    capped = IOSSimulatorPool(
        golden="G", runner=FakeSimctl(), resources=HostResources(36 * GB, 12),
        policy=CapacityPolicy(max_simulators=3),
    )
    assert capped.capacity("ios") == 3


def test_ios_pool_needs_a_source():
    with pytest.raises(ValueError):
        IOSSimulatorPool(runner=FakeSimctl())


# Tart VM pool


def test_tart_pool_clones_runs_waits_and_reports_the_ip(tmp_path: Path):
    runner = FakeTart()
    pool = TartVMPool(image="base", runner=runner, capacity=2, work_root=tmp_path)
    device = pool.acquire("macos", MAC_BUILD)
    assert device.kind == "vm"
    assert device.id == "aqa-vm-0"
    assert device.address == "192.168.64.10"
    assert device.meta["guest_app_path"] == "/Volumes/My Shared Files/aqa-build/Sample.app"
    assert runner.calls == [
        ["tart", "list", "--format", "json"],
        ["tart", "clone", "base", "aqa-vm-0"],
        ["tart", "run", "--no-graphics", "--dir=aqa-build:/builds/mac", "aqa-vm-0"],
        ["tart", "ip", "aqa-vm-0"],
        ["tart", "exec", "aqa-vm-0", "true"],
    ]
    pool.release(device, erase=False)
    assert runner.calls[-1] == ["tart", "stop", "aqa-vm-0"]
    assert pool.acquire("macos", None).id == "aqa-vm-0"
    assert len(runner.verbs("clone")) == 1
    pool.close()
    assert runner.verbs("delete") == [["tart", "delete", "aqa-vm-0"]]


def test_tart_pool_erase_deletes_and_reclones(tmp_path: Path):
    runner = FakeTart()
    pool = TartVMPool(image="base", runner=runner, capacity=1, work_root=tmp_path)
    device = pool.acquire("macos", None)
    pool.release(device, erase=True)
    assert runner.calls[-2:] == [["tart", "stop", "aqa-vm-0"], ["tart", "delete", "aqa-vm-0"]]
    pool.acquire("macos", None)
    assert len(runner.verbs("clone")) == 2


def test_tart_pool_holds_the_slot_while_a_vm_will_not_stop(tmp_path: Path):
    runner = FakeTart(stop_fails=True)
    pool = TartVMPool(image="base", runner=runner, capacity=1, work_root=tmp_path, poll_s=0.01)
    pool.release(pool.acquire("macos", None), erase=False)
    with pytest.raises(DeviceUnavailable):
        pool.acquire("macos", None, timeout_s=0.03)


def test_tart_pool_never_reuses_a_vm_it_failed_to_erase(tmp_path: Path):
    runner = FakeTart(delete_fails=True)
    pool = TartVMPool(image="base", runner=runner, capacity=1, work_root=tmp_path)
    pool.release(pool.acquire("macos", None), erase=True)
    with pytest.raises(DeviceSetupError, match="stale VM"):
        pool.acquire("macos", None, timeout_s=0)
    runner.delete_fails = False
    pool.acquire("macos", None, timeout_s=0)
    assert len(runner.verbs("clone")) == 2


def test_tart_pool_readiness_timeout_raises_and_frees_the_slot(tmp_path: Path):
    runner = FakeTart(agent_never_ready=True)
    now = [0.0]
    pool = TartVMPool(
        image="base", runner=runner, capacity=1, boot_timeout_s=20, poll_s=5, work_root=tmp_path,
        sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0],
    )
    with pytest.raises(DeviceUnavailable, match="not ready after 20s"):
        pool.acquire("macos", None)
    assert runner.calls[-1] == ["tart", "stop", "aqa-vm-0"]
    assert len(runner.verbs("exec")) >= 4
    runner.agent_never_ready = False
    assert pool.acquire("macos", None, timeout_s=0).address


def test_tart_pool_capacity_never_exceeds_two():
    assert TartVMPool(image="base", runner=FakeTart(), capacity=5).capacity("macos") == 2
    assert TartVMPool(image="base", runner=FakeTart(), resources=HostResources(512 * GB, 64)).capacity("macos") == 2


def test_tart_pool_and_backend_share_the_two_host_slots(tmp_path: Path):
    pool = TartVMPool(image="base", runner=FakeTart(), capacity=2, work_root=tmp_path)
    devices = [pool.acquire("macos", None) for _ in range(2)]
    config = sample_config()
    config.vm.image = "base"
    now = [0.0]
    backend_tart = FakeTart()
    backend = TartBackend(
        config, runner=backend_tart, resources=HostResources(512 * GB, 64), slot_timeout_s=3, poll_s=1,
        sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0],
    )
    result = backend.run_shard(scripted_shard(), "w1", campaign_dir=tmp_path / "camp")
    assert result.status == "error"
    assert "at most 2 macOS VMs" in (result.error or "")
    assert backend_tart.calls == []
    pool.release(devices[0])
    slot = try_lock(f"{TART_SLOT_PREFIX}-0")
    assert slot is not None
    slot.release()
    pool.close()


# Local Mac pool


def test_local_mac_pool_leases_the_host(tmp_path: Path):
    runner = FakeHostRunner()
    pool = LocalMacPool(runner=runner, platform="darwin", work_root=tmp_path)
    device = pool.acquire("macos", MAC_BUILD)
    assert (device.id, device.kind, device.platform, device.address) == ("host", "host", "macos", "")
    pool.release(device, erase=True)
    assert runner.calls == []  # the user's own Mac: no reset unless asked


def test_local_mac_pool_reset_runs_defaults_delete(tmp_path: Path):
    runner = FakeHostRunner()
    pool = LocalMacPool(runner=runner, platform="darwin", reset_app_state=True, work_root=tmp_path)
    pool.release(pool.acquire("macos", MAC_BUILD))
    assert runner.calls == [["defaults", "delete", "dev.swarmqa.sample"]]


def test_local_mac_pool_is_empty_off_macos():
    pool = LocalMacPool(runner=FakeHostRunner(), platform="linux")
    assert pool.capacity("macos") == 0
    with pytest.raises(DeviceUnavailable):
        pool.acquire("macos", None, timeout_s=0)
