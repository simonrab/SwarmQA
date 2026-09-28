"""Driver factory.

`kind="fake"` is the default off macOS so campaigns and tests have a session
without Accessibility permission. `kind="macos"` defers to the macOS driver.
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
    selected = kind or ("macos" if sys.platform == "darwin" else "fake")
    if selected == "fake":
        return FakeDriver(target, work_dir, video_mode=video_mode)
    if selected == "macos":
        from swarmqa.driver.macos import MacOSDriver

        return MacOSDriver(target, work_dir, video_mode=video_mode)
    raise ValueError(f"unknown driver kind: {selected}")
