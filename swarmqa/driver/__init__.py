"""Driver factory.

`kind="fake"` is the default off macOS so campaigns and tests have a session
without Accessibility permission. `kind="macos"` defers to the macOS driver.
`kind="ios"` defers to the iOS Simulator driver. `app.platform = "ios"` selects
that driver even when `kind` is omitted. `kind="legacy"` picks the macOS or iOS
driver from `app.platform`. `kind="runner"` drives the app through the XCUITest
swarm runner (`agents/swarm-runner`): `IOSRunnerDriver` when `app.platform` is
`ios`, else `MacOSRunnerDriver`. Both implement AppDriver v2.
"""

from __future__ import annotations

import sys
from pathlib import Path

from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import AppDriver
from swarmqa.models import AppTarget


def create_driver(
    target: AppTarget,
    work_dir: Path,
    *,
    kind: str | None = None,
    video_mode: str = "always",
    udid: str | None = None,
) -> AppDriver:
    """`udid` pins an iOS driver to a simulator leased from a DevicePool (no lock taken)."""
    if kind == "legacy":
        selected = "ios" if target.platform == "ios" else "macos"
    elif kind == "runner":
        if target.platform == "ios":
            from swarmqa.driver.ios_runner import IOSRunnerDriver

            return IOSRunnerDriver(target, work_dir, video_mode=video_mode, udid=udid)
        from swarmqa.driver.macos_runner import MacOSRunnerDriver

        return MacOSRunnerDriver(target, work_dir, video_mode=video_mode)
    elif kind:
        selected = kind
    elif target.platform == "ios":
        selected = "ios"
    else:
        selected = "macos" if sys.platform == "darwin" else "fake"
    if selected == "fake":
        return FakeDriver(target, work_dir, video_mode=video_mode)
    if selected == "macos":
        from swarmqa.driver.macos import MacOSDriver

        return MacOSDriver(target, work_dir, video_mode=video_mode)
    if selected == "ios":
        from swarmqa.driver.ios import IOSSimulatorDriver

        return IOSSimulatorDriver(target, work_dir, video_mode=video_mode, udid=udid)
    raise ValueError(f"unknown driver kind: {selected}")
