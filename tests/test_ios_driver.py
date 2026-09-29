"""Linux-safe tests for the iOS Simulator driver.

Xcode is not required. Tests inject ``driver.runner`` and set
``platform="darwin"`` when they need to exercise simctl and idb output.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path

import pytest

from swarmqa.driver import create_driver
from swarmqa.driver.ios import IOSSimulatorDriver, parse_idb_tree, select_simulator
from swarmqa.errors import (
    AppCrashedError,
    AppMissingError,
    BackendUnavailable,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import AppTarget, ElementQuery

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
)

BOOTED = "11111111-1111-1111-1111-111111111111"
NEWER = "22222222-2222-2222-2222-222222222222"
PINNED = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

DEVICES = {
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
            {
                "udid": BOOTED,
                "name": "iPhone 17",
                "state": "Booted",
                "isAvailable": True,
            }
        ],
        "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
            {
                "udid": NEWER,
                "name": "iPhone 17",
                "state": "Shutdown",
                "isAvailable": True,
            },
            {
                "udid": "33333333-3333-3333-3333-333333333333",
                "name": "iPhone SE",
                "state": "Shutdown",
                "isAvailable": False,
            },
        ],
    }
}

TREE = json.dumps(
    [
        {
            "AXLabel": "Sample",
            "type": "Window",
            "frame": {"x": 0, "y": 0, "width": 390, "height": 844},
            "enabled": True,
            "children": [
                {
                    "AXLabel": "Save Draft",
                    "AXUniqueId": "save",
                    "type": "Button",
                    "frame": {"x": 10, "y": 20, "width": 80, "height": 30},
                    "enabled": True,
                    "children": [],
                },
                {
                    "AXLabel": "Email",
                    "AXValue": "user@example.com",
                    "type": "TextField",
                    "frame": {"x": 10, "y": 60, "width": 200, "height": 24},
                    "enabled": True,
                    "children": [],
                },
            ],
        }
    ]
)


class FakeProcess:
    def __init__(self, code: int | None = None):
        self.pid = 77
        self.code = code
        self.signals: list[str] = []

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int | None:
        if self.code is None:
            self.code = 0
        return self.code

    def kill(self) -> None:
        self.signals.append("kill")
        self.code = -9


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.kw: list[dict] = []
        self.tools = {"xcrun": "/usr/bin/xcrun", "idb": "/usr/bin/idb"}
        self.devices = DEVICES
        self.boot_code = 0
        self.boot_stderr = ""
        self.launch_code = 0
        self.launch_stdout = "dev.swarmqa.sample: 4242\n"
        self.launch_stderr = ""
        self.alive = True
        self.tree = TREE
        self.install_code = 0
        self.install_stderr = ""
        self.video_dies = False
        self.build_code = 0
        self.build_stdout = ""
        self.build_stderr = ""
        self.action_code = 0
        self.action_stderr = ""
        self.processes: list[FakeProcess] = []

    def __call__(self, args: list[str], **kwargs):
        args = list(args)
        self.calls.append(args)
        self.kw.append(kwargs)
        if args[0] == "which":
            found = self.tools.get(args[1])
            if not found:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=found + "\n", stderr="")
        if args[:2] == ["/bin/sh", "-c"]:
            return subprocess.CompletedProcess(
                args, self.build_code, stdout=self.build_stdout, stderr=self.build_stderr
            )
        if args[:2] == ["xcrun", "simctl"]:
            return self._simctl(args, kwargs)
        if args[0] == "idb":
            return self._idb(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _simctl(self, args: list[str], kwargs: dict):
        command = args[2]
        if command == "list":
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.devices), stderr="")
        if command == "boot":
            return subprocess.CompletedProcess(args, self.boot_code, stdout="", stderr=self.boot_stderr)
        if command == "bootstatus":
            return subprocess.CompletedProcess(args, 0, stdout="Device already booted\n", stderr="")
        if command == "install":
            return subprocess.CompletedProcess(
                args, self.install_code, stdout="", stderr=self.install_stderr
            )
        if command == "launch":
            return subprocess.CompletedProcess(
                args, self.launch_code, stdout=self.launch_stdout, stderr=self.launch_stderr
            )
        if command == "spawn":
            code = 0 if self.alive else 1
            err = "" if self.alive else "app exited"
            return subprocess.CompletedProcess(args, code, stdout="", stderr=err)
        if command == "terminate":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if command == "io" and "screenshot" in args:
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PNG)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if command == "io" and "recordVideo" in args:
            proc = FakeProcess(code=0 if self.video_dies else None)
            self.processes.append(proc)
            return proc
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _idb(self, args: list[str]):
        if "describe-all" in args:
            return subprocess.CompletedProcess(args, 0, stdout=self.tree, stderr="")
        if self.action_code != 0:
            return subprocess.CompletedProcess(args, self.action_code, stdout="", stderr=self.action_stderr)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def write_app(path: Path, *, bundle_id: str = "dev.swarmqa.sample", version: str = "1.4.0") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with (path / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": version,
                "CFBundleName": "Sample",
            },
            handle,
        )
    return path


def driver_for(tmp_path: Path, app: Path | None = None, **kwargs) -> tuple[IOSSimulatorDriver, FakeRunner]:
    runner = kwargs.pop("runner", None) or FakeRunner()
    target = kwargs.pop(
        "target",
        AppTarget(
            path=str(app or write_app(tmp_path / "Sample.app")),
            bundle_id="dev.swarmqa.sample",
            platform="ios",
            env={"TOKEN": "1"},
            launch_args=["--safe"],
        ),
    )
    driver = IOSSimulatorDriver(
        target,
        tmp_path / "work",
        runner=runner,
        platform=kwargs.pop("platform", "darwin"),
        **kwargs,
    )
    return driver, runner


def commands(runner: FakeRunner) -> list[list[str]]:
    return [call for call in runner.calls if call[:2] == ["xcrun", "simctl"]]


def test_parse_idb_tree_maps_roles_frames_and_children():
    nodes = parse_idb_tree(TREE)
    assert nodes[0].role == "window"
    button = nodes[0].children[0]
    assert button.role == "button"
    assert button.label == "Save Draft"
    assert button.identifier == "save"
    assert button.frame == (10, 20, 80, 30)
    field = nodes[0].children[1]
    assert field.role == "textfield"
    assert field.value == "user@example.com"
    text_frame = parse_idb_tree(
        json.dumps(
            [
                {
                    "type": "XCUIElementTypeButton",
                    "AXLabel": "Go",
                    "AXFrame": "{{1, 2}, {3, 4}}",
                }
            ]
        )
    )
    assert text_frame[0].role == "button"
    assert text_frame[0].frame == (1, 2, 3, 4)


def test_select_simulator_prefers_booted_then_newer_runtime():
    assert select_simulator(DEVICES, "iPhone 17") == BOOTED
    shutdown = json.loads(json.dumps(DEVICES))
    shutdown["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-17-5"][0]["state"] = "Shutdown"
    assert select_simulator(shutdown, "iPhone 17") == NEWER
    with pytest.raises(BackendUnavailable, match="iPhone 99"):
        select_simulator(DEVICES, "iPhone 99")


def test_non_darwin_refuses_before_any_command(tmp_path: Path):
    driver, runner = driver_for(tmp_path, platform="linux")
    with pytest.raises(BackendUnavailable, match="fake driver"):
        driver.launch()
    assert runner.calls == []


def test_missing_xcrun_names_xcode(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    runner.tools.pop("xcrun")
    with pytest.raises(BackendUnavailable, match="Xcode"):
        driver.launch()


def test_missing_bundle(tmp_path: Path):
    target = AppTarget(path=str(tmp_path / "Missing.app"), platform="ios")
    driver, _runner = driver_for(tmp_path, target=target)
    with pytest.raises(AppMissingError, match="app not found"):
        driver.launch()


def test_launch_boots_installs_and_passes_child_env(tmp_path: Path):
    app = write_app(tmp_path / "Sample.app")
    driver, runner = driver_for(tmp_path, app)
    driver.launch()
    simctl = commands(runner)
    assert ["xcrun", "simctl", "boot", BOOTED] in simctl
    assert ["xcrun", "simctl", "bootstatus", BOOTED, "-b"] in simctl
    assert ["xcrun", "simctl", "install", BOOTED, str(app)] in simctl
    launch = next(call for call in simctl if call[2] == "launch")
    assert launch[3:] == [BOOTED, "dev.swarmqa.sample", "--safe"]
    launch_env = runner.kw[runner.calls.index(launch)]["env"]
    assert launch_env["SIMCTL_CHILD_TOKEN"] == "1"
    assert driver.udid == BOOTED
    assert driver.launched


def test_already_booted_is_success(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    runner.boot_code = 149
    runner.boot_stderr = "Unable to boot device in current state: Booted"
    driver.launch()
    assert driver.launched


def test_pinned_udid_skips_device_list(tmp_path: Path):
    driver, runner = driver_for(tmp_path, udid=PINNED)
    driver.launch()
    assert driver.udid == PINNED
    assert not any(call[2] == "list" for call in commands(runner))
    assert ["xcrun", "simctl", "boot", PINNED] in commands(runner)


def test_build_command_discovers_app(tmp_path: Path):
    app = write_app(tmp_path / "Built.app")
    runner = FakeRunner()
    runner.build_stdout = f"built {app}\n"
    target = AppTarget(build_command="echo app", platform="ios", bundle_id="dev.swarmqa.sample")
    driver, _runner = driver_for(tmp_path, target=target, runner=runner)
    driver.launch()
    assert driver.metadata().path == str(app)


def test_crash_on_launch(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    runner.launch_code = 1
    runner.launch_stderr = "app crashed on launch"
    with pytest.raises(AppCrashedError, match="crashed"):
        driver.launch()


def test_click_type_scroll_menu_and_screenshot(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    driver.launch()
    driver.click(ElementQuery(label="Save Draft"))
    driver.type_text(ElementQuery(label="Email"), "ada@example.com")
    driver.scroll(1)
    driver.select_menu(["Save Draft"])
    driver.keychord(["cmd+return"])
    shot = driver.screenshot("home")
    taps = [call for call in runner.calls if call[:3] == ["idb", "--udid", BOOTED] and "tap" in call]
    assert ["idb", "--udid", BOOTED, "ui", "tap", "50", "35"] in taps
    assert ["idb", "--udid", BOOTED, "ui", "text", "ada@example.com"] in runner.calls
    swipe = next(call for call in runner.calls if "swipe" in call)
    assert swipe[:6] == ["idb", "--udid", BOOTED, "ui", "swipe", "195"]
    assert ["idb", "--udid", BOOTED, "ui", "key", "40"] in runner.calls
    assert shot == tmp_path / "work" / "media" / "home.png"
    assert shot.read_bytes() == PNG


def test_missing_idb_on_tree(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    driver.launch()
    runner.tools.pop("idb")
    with pytest.raises(BackendUnavailable, match="idb"):
        driver.accessibility_tree()


def test_wait_timeout_and_missing_element(tmp_path: Path):
    driver, _runner = driver_for(tmp_path)
    driver.launch()
    driver._poll_s = 0
    with pytest.raises(UITimeoutError):
        driver.wait_for(ElementQuery(label="Missing"), 0)
    with pytest.raises(ElementNotFoundError):
        driver.click(ElementQuery(label="Missing"))


def test_crash_during_action(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    driver.launch()
    runner.alive = False
    with pytest.raises(AppCrashedError):
        driver.click(ElementQuery(label="Save Draft"))


def test_video_and_close_terminate(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    driver.launch()
    driver.start_video()
    path = driver.stop_video()
    assert path == tmp_path / "work" / "media" / "session.mp4"
    assert runner.processes[0].signals == ["kill"]
    driver.close()
    driver.close()
    terminates = [call for call in commands(runner) if call[2] == "terminate"]
    assert terminates == [["xcrun", "simctl", "terminate", BOOTED, "dev.swarmqa.sample"]]


def test_video_failure_writes_note(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    runner.video_dies = True
    driver.launch()
    driver.start_video()
    note = driver.stop_video()
    assert note is not None
    assert note.name == "session.txt"
    assert "unavailable" in note.read_text(encoding="utf-8")


def test_metadata_reads_ios_plist_without_a_session(tmp_path: Path):
    app = write_app(tmp_path / "Sample.app", version="9.0")
    driver, _runner = driver_for(tmp_path, app)
    meta = driver.metadata()
    assert meta.backend == "ios"
    assert meta.bundle_id == "dev.swarmqa.sample"
    assert meta.version == "9.0"
    assert meta.path == str(app)


def test_relaunch_terminates_and_launches_again(tmp_path: Path):
    driver, runner = driver_for(tmp_path)
    driver.launch()
    driver.relaunch()
    launches = [call for call in commands(runner) if call[2] == "launch"]
    installs = [call for call in commands(runner) if call[2] == "install"]
    assert len(launches) == 2
    assert len(installs) == 1
    assert any(call[2] == "terminate" for call in commands(runner))
    assert driver.launched


def test_simulator_pool_assigns_one_device_per_worker(tmp_path: Path):
    app = write_app(tmp_path / "Sample.app")
    shared = AppTarget(path=str(app), platform="ios", simulators=["iPhone 17"])
    workers = tmp_path / "workers"
    first = IOSSimulatorDriver(shared, workers / "a", runner=FakeRunner(), platform="darwin")
    second = IOSSimulatorDriver(shared, workers / "b", runner=FakeRunner(), platform="darwin")
    first.launch()
    with pytest.raises(BackendUnavailable, match="No free iOS Simulator"):
        second.launch()
    first.close()
    second.launch()
    assert second.udid == BOOTED
    second.close()

    distinct = AppTarget(path=str(app), platform="ios", simulators=["iPhone 17", PINNED])
    alpha = IOSSimulatorDriver(distinct, workers / "c", runner=FakeRunner(), platform="darwin")
    beta = IOSSimulatorDriver(distinct, workers / "d", runner=FakeRunner(), platform="darwin")
    alpha.launch()
    beta.launch()
    assert alpha.udid == BOOTED
    assert beta.udid == PINNED


def test_stale_simulator_lock_is_reclaimed(tmp_path: Path):
    app = write_app(tmp_path / "Sample.app")
    target = AppTarget(path=str(app), platform="ios", simulators=["iPhone 17"])
    driver, _runner = driver_for(tmp_path, target=target)
    lock_dir = tmp_path / ".ios-simulators"
    lock_dir.mkdir(parents=True)
    (lock_dir / "0-iPhone-17.lock").write_text("999999\n", encoding="utf-8")
    driver.launch()
    assert driver.launched
    assert driver.udid == BOOTED


def test_create_driver_uses_ios_for_platform_and_kind(tmp_path: Path):
    target = AppTarget(path=str(tmp_path / "Sample.app"), platform="ios")
    selected = create_driver(target, tmp_path / "work")
    assert isinstance(selected, IOSSimulatorDriver)
    explicit = create_driver(AppTarget(), tmp_path / "kind", kind="ios")
    assert isinstance(explicit, IOSSimulatorDriver)
    with pytest.raises(ValueError, match="unknown driver kind"):
        create_driver(AppTarget(), tmp_path / "bad", kind="android")


def test_driver_and_pool_share_the_udid_lock(tmp_path: Path):
    from swarmqa.devices.locks import simulator_lock_name, try_lock

    app = write_app(tmp_path / "Sample.app")
    target = AppTarget(path=str(app), platform="ios", simulators=["iPhone 17"])
    held = try_lock(simulator_lock_name(BOOTED))
    driver = IOSSimulatorDriver(target, tmp_path / "w", runner=FakeRunner(), platform="darwin")
    with pytest.raises(BackendUnavailable, match="No free iOS Simulator"):
        driver.launch()
    held.release()
    driver.launch()
    assert driver.udid == BOOTED
    driver.close()
