"""macOS app driver over the swarm runner (`driver.kind = "runner"`).

`MacOSRunnerDriver` starts the macOS XCUITest runner (`RunnerProcess`) on the
local Mac and drives the app through the runner's HTTP protocol. The app runs
in place: `app.path` is registered with LaunchServices and launched by its
bundle id. It implements AppDriver v1 and v2.

`/observe` asks for JPEG by default, because the macOS runner captures the
whole main display and a PNG of it is several megabytes. `screenshot()` stays
PNG for baselines.

The runner needs UI automation mode. The first time, macOS asks for an
administrator password; without approval xcodebuild fails with "Timed out
while enabling automation mode", which surfaces as `BackendUnavailable`
telling the user to approve the prompt or run
`automationmodetool enable-automationmode-without-authentication`.

Like the legacy macOS driver, this takes no lock on the desktop: one runner
drives the real mouse and keyboard, so run one macOS session per desktop (or
per VM).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from swarmqa.devices import commands
from swarmqa.driver.macos import _as_bundle
from swarmqa.driver.runner_client import (
    RunnerDriverBase,
    RunnerProcess,
    log_start_arg,
    read_info_plist,
)
from swarmqa.errors import AppMissingError, BackendUnavailable

_NON_DARWIN = "MacOSRunnerDriver needs a Mac with Xcode. Use the fake driver on this host."
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


class MacOSRunnerDriver(RunnerDriverBase):
    """Drive a macOS app on this Mac through the swarm runner."""

    backend = "macos-runner"
    default_screenshot_format = "jpeg"
    scroll_distance = 120.0

    # Hooks

    def _ensure_host(self) -> None:
        if self.platform != "darwin":
            raise BackendUnavailable(_NON_DARWIN)

    def _prepare(self) -> None:
        configured = (getattr(self.target, "path", None) or "").strip()
        if configured:
            bundle = _as_bundle(Path(configured).expanduser())
            if not bundle.is_dir() or bundle.suffix != ".app":
                raise AppMissingError(f"app not found or not a .app bundle: {bundle}")
            info = read_info_plist(bundle)
            self.bundle_path = bundle
            self.bundle_id = str(info.get("CFBundleIdentifier") or "").strip() or None
            self.process_name = str(info.get("CFBundleExecutable") or "").strip() or bundle.stem
            self.version = str(info.get("CFBundleShortVersionString") or "").strip() or None
            # The runner launches by bundle id; make sure LaunchServices knows
            # this copy. Best effort: a failure here still leaves launch to try.
            commands.run(self.runner, [LSREGISTER, "-f", str(bundle)], timeout=60)
        configured_id = (getattr(self.target, "bundle_id", None) or "").strip()
        self.bundle_id = self.bundle_id or configured_id or None
        if not self.bundle_id:
            raise AppMissingError("set app.path to a macOS .app, or app.bundle_id for an installed app")
        if not self.process_name:
            self.process_name = self.bundle_id.rsplit(".", 1)[-1]

    def _make_process(self) -> RunnerProcess:
        return RunnerProcess(
            "macos",
            self.work_dir,
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
        # The same bundle built for the simulator crashes into the same folder.
        return "CoreSimulator" not in str(body.get("procPath") or "")

    def _log_command(self, ts: float) -> list[str] | None:
        if not self.process_name:
            return None
        return [
            "/usr/bin/log",
            "show",
            "--style",
            "ndjson",
            "--start",
            log_start_arg(ts),
            "--predicate",
            f'process == "{self.process_name}"',
        ]

    def start_video(self) -> None:
        if self._video_on:
            return
        self._client()
        path = self._media_dir() / "session.mov"
        # Needs Screen Recording permission for the host process.
        self._start_video_process(["/usr/sbin/screencapture", "-v", "-x", str(path)], path)

    def stop_video(self) -> Path | None:
        return self._stop_video_process()
