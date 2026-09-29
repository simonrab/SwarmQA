"""Driver factory.

`kind="fake"` is the default off macOS so campaigns and tests have a session
without Accessibility permission. `kind="macos"` defers to the macOS driver.
`kind="ios"` defers to the iOS Simulator driver. `app.platform = "ios"` selects
that driver even when `kind` is omitted. `kind="legacy"` picks the macOS or iOS
driver from `app.platform`. `kind="runner"` is reserved for the XCUITest runner.
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
) -> AppDriver:
    if kind == "legacy":
        selected = "ios" if target.platform == "ios" else "macos"
    elif kind == "runner":
        raise ValueError("driver kind runner is not available yet")
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

        return IOSSimulatorDriver(target, work_dir, video_mode=video_mode)
    raise ValueError(f"unknown driver kind: {selected}")
