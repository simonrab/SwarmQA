"""C2 — macOS Accessibility driver.

Replace the stub. Keep the class name and method set compatible with
swarmqa.driver.protocol.AppDriver. See docs/CONTRACTS.md section C2.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady
from swarmqa.models import AppTarget


class MacOSDriver:
    def __init__(self, target: AppTarget, work_dir: Path, video_mode: str = "always"):
        self.target = target
        self.work_dir = Path(work_dir)
        self.video_mode = video_mode

    def launch(self) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.launch")

    def relaunch(self) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.relaunch")

    def close(self) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.close")

    def accessibility_tree(self):
        raise ChunkNotReady("C2", "MacOSDriver.accessibility_tree")

    def click(self, target) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.click")

    def type_text(self, target, text: str) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.type_text")

    def keychord(self, keys: list[str]) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.keychord")

    def scroll(self, delta: int, target=None) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.scroll")

    def select_menu(self, path: list[str]) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.select_menu")

    def wait_for(self, target, timeout_s: float):
        raise ChunkNotReady("C2", "MacOSDriver.wait_for")

    def screenshot(self, name: str) -> Path:
        raise ChunkNotReady("C2", "MacOSDriver.screenshot")

    def start_video(self) -> None:
        raise ChunkNotReady("C2", "MacOSDriver.start_video")

    def stop_video(self) -> Path | None:
        raise ChunkNotReady("C2", "MacOSDriver.stop_video")

    def metadata(self):
        raise ChunkNotReady("C2", "MacOSDriver.metadata")
