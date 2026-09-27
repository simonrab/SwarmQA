"""App driver protocol.

One driver instance controls one app session inside one worker environment.
It does not take an exclusive lock on the host display.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from swarmqa.models import BuildMetadata, ElementQuery, UIElement


class AppDriver(Protocol):
    def launch(self) -> None:
        """Launch the target app. Raise AppMissingError or AppCrashedError."""

    def relaunch(self) -> None:
        """Quit and launch again when the planner asks for a fresh process."""

    def close(self) -> None:
        """Release the session. Safe to call more than once."""

    def accessibility_tree(self) -> list[UIElement]:
        """Return windows, buttons, text fields, and menus currently visible."""

    def click(self, target: ElementQuery) -> None: ...

    def type_text(self, target: ElementQuery, text: str) -> None: ...

    def keychord(self, keys: list[str]) -> None: ...

    def scroll(self, delta: int, target: ElementQuery | None = None) -> None: ...

    def select_menu(self, path: list[str]) -> None: ...

    def wait_for(self, target: ElementQuery, timeout_s: float) -> UIElement: ...

    def screenshot(self, name: str) -> Path: ...

    def start_video(self) -> None: ...

    def stop_video(self) -> Path | None: ...

    def metadata(self) -> BuildMetadata: ...
