"""iOS Simulator driver over the swarm runner (`driver.kind = "runner"`).

`IOSRunnerDriver` resolves one simulator, boots it, installs the `.app`,
starts the XCUITest runner for that simulator (`RunnerProcess`) and drives the
app through the runner's HTTP protocol. It implements AppDriver v1 and v2;
`observe` is one `/observe` round-trip.

Simulator resolution follows `swarmqa.driver.ios`:

1. the `udid` argument, then `SWARMQA_SIMULATOR_UDID` (no lock: the pool that
   handed out the UDID holds `simulator-<udid>`);
2. the first free entry of `app.simulators`, locked host-wide;
3. `app.simulator` (a UDID or a device name, default `iPhone 17`), also locked,
   because a second runner on the same simulator would fight the first.

Subprocess calls go through `runner` (the `swarmqa.devices.commands` seam)
and the xcodebuild and video processes through `spawn`, so Linux tests need
no Xcode. `launch` on a host that is not macOS raises `BackendUnavailable`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from swarmqa.devices import commands
from swarmqa.devices.locks import HostLock, simulator_lock_name, try_lock
from swarmqa.driver.ios import _DEFAULT_DEVICE, _UDID_ENV, _discover_app_path, _is_udid, select_simulator
from swarmqa.driver.runner_client import (
    RunnerDriverBase,
    RunnerProcess,
    log_start_arg,
    read_info_plist,
)
from swarmqa.errors import AppMissingError, BackendUnavailable

_NON_DARWIN = (
    "IOSRunnerDriver needs a Mac with Xcode and an iOS Simulator runtime. "
    "Use the fake driver on this host."
)


class IOSRunnerDriver(RunnerDriverBase):
    """Drive an iOS app in the Simulator through the swarm runner."""

    backend = "ios-runner"
    default_screenshot_format = "png"

    def __init__(self, target: Any, work_dir: Path, video_mode: str = "always", *, udid: str | None = None, **kwargs: Any):
        super().__init__(target, work_dir, video_mode, **kwargs)
        self._udid_override = (udid or "").strip() or None
        self.udid: str | None = None
        self._lock: HostLock | None = None
        self.boot_timeout_s = 600.0

    # Hooks

    def _ensure_host(self) -> None:
        if self.platform != "darwin":
            raise BackendUnavailable(_NON_DARWIN)

    def _prepare(self) -> None:
        bundle = self._bundle()
        if self.udid is None:
            self.udid = self._claim_simulator()
        self._boot(self.udid)
        if bundle is not None:
            self._install(self.udid, bundle)

    def _make_process(self) -> RunnerProcess:
        assert self.udid
        return RunnerProcess(
            "ios",
            self.work_dir,
            udid=self.udid,
            port=self.port,
            derived_data=self.derived_data,
            auto_build=self.auto_build,
            ready_timeout_s=self.ready_timeout_s,
            runner=self.runner,
            spawn=self.spawn,
            timeouts=self.timeouts,
            sleep=self.sleep,
        )

    def _crash_matches(self, header: dict[str, Any], body: dict[str, Any]) -> bool:
        # Simulator crashes land in the host's DiagnosticReports with a
        # procPath under CoreSimulator/Devices/<udid>/.
        path = str(body.get("procPath") or "")
        if "CoreSimulator" not in path:
            return False
        if self.udid and "/Devices/" in path:
            return self.udid.lower() in path.lower()
        return True

    def _log_command(self, ts: float) -> list[str] | None:
        if not self.udid or not self.process_name:
            return None
        return [
            "xcrun",
            "simctl",
            "spawn",
            self.udid,
            "log",
            "show",
            "--style",
            "ndjson",
            "--start",
            log_start_arg(ts),
            "--predicate",
            f'process == "{self.process_name}"',
        ]

    def _release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()
            self.udid = None

    def start_video(self) -> None:
        if self._video_on:
            return
        self._client()
        path = self._media_dir() / "session.mp4"
        self._start_video_process(
            ["xcrun", "simctl", "io", self.udid or "", "recordVideo", "--force", str(path)],
            path,
        )

    def stop_video(self) -> Path | None:
        return self._stop_video_process()

    # Internals

    def _bundle(self) -> Path | None:
        configured = (getattr(self.target, "path", None) or "").strip()
        bundle: Path | None = None
        if configured:
            bundle = Path(configured).expanduser()
        elif (getattr(self.target, "build_command", None) or "").strip():
            result = commands.run(self.runner, ["/bin/sh", "-c", self.target.build_command], timeout=1800)
            if not result.ok:
                raise AppMissingError(result.detail())
            found = _discover_app_path(result.stdout + "\n" + result.stderr)
            if not found:
                raise AppMissingError("build command finished but did not report a .app path; set app.path")
            bundle = Path(found)
        if bundle is not None:
            if not bundle.is_dir() or bundle.suffix != ".app":
                raise AppMissingError(f"app not found or not a .app bundle: {bundle}")
            info = read_info_plist(bundle)
            self.bundle_path = bundle
            self.bundle_id = str(info.get("CFBundleIdentifier") or "").strip() or None
            self.process_name = str(info.get("CFBundleExecutable") or "").strip() or bundle.stem
            self.version = str(info.get("CFBundleShortVersionString") or "").strip() or None
        configured_id = (getattr(self.target, "bundle_id", None) or "").strip()
        self.bundle_id = self.bundle_id or configured_id or None
        if not self.bundle_id:
            raise AppMissingError("set app.path to a simulator .app, or app.bundle_id for an installed app")
        return bundle

    def _claim_simulator(self) -> str:
        pinned = self._udid_override or (os.environ.get(_UDID_ENV) or "").strip()
        if pinned:
            if _is_udid(pinned):
                return pinned
            return select_simulator(self._devices(), pinned)
        pool = [item.strip() for item in getattr(self.target, "simulators", []) or [] if item.strip()]
        pool = list(dict.fromkeys(pool))
        if not pool:
            pool = [(getattr(self.target, "simulator", None) or "").strip() or _DEFAULT_DEVICE]
        document: dict | None = None
        busy = []
        for name in pool:
            if _is_udid(name):
                udid = name
            else:
                if document is None:
                    document = self._devices()
                udid = select_simulator(document, name)
            lock = try_lock(simulator_lock_name(udid))
            if lock is None:
                busy.append(name)
                continue
            self._lock = lock
            return udid
        raise BackendUnavailable(
            f"No free iOS Simulator: {', '.join(busy)} already in use by another worker. "
            "Add one device name or UDID per worker to app.simulators."
        )

    def _devices(self) -> dict:
        import json

        result = commands.run(self.runner, ["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=60)
        if not result.ok:
            raise BackendUnavailable(f"simctl list failed: {result.detail()}")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise BackendUnavailable("simctl list did not return a device list") from exc

    def _boot(self, udid: str) -> None:
        # bootstatus -b boots a shut-down device and returns at once when booted.
        result = commands.run(self.runner, ["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=self.boot_timeout_s)
        if not result.ok:
            raise BackendUnavailable(f"Simulator {udid} did not boot: {result.detail()}")

    def _install(self, udid: str, bundle: Path) -> None:
        result = commands.run(self.runner, ["xcrun", "simctl", "install", udid, str(bundle)], timeout=300)
        if not result.ok:
            raise AppMissingError(f"simctl install {bundle} failed: {result.detail()}")
