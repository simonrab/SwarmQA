"""App driver protocol.

One driver instance controls one app session inside one worker environment.
It does not take an exclusive lock on the host display.

`AppDriver` is the v1 protocol every driver implements. `AppDriverV2` adds
one-round-trip observation, coordinate gestures, and log and crash queries
for the agent loop. Coordinates are points in the same space as
`UIElement.frame`: origin at the top-left of the screen (iOS) or of the main
display (macOS). Timestamps are Unix epoch seconds so they compare with
device logs and crash reports.

`supports_v2` tells the two apart. Drivers that only speak v1 can be wrapped
with `swarmqa.driver.v1_adapter.V1Adapter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from swarmqa.models import BuildMetadata, ElementQuery, UIElement

LogLevel = Literal["debug", "info", "notice", "error", "fault"]


class UnsupportedAction(Exception):
    """The driver cannot perform this action (for example a v1 driver asked to swipe)."""


@dataclass
class ScreenObservation:
    """Everything the agent loop needs about the screen at one instant.

    `tree` and `screenshot` are captured in one driver round-trip so they
    describe the same frame. `screenshot` is None when the caller asked for
    the tree only. `size` is the screen or window size in points and `scale`
    is pixels per point in the screenshot.
    """

    tree: list[UIElement]
    ts: float
    screenshot: Path | None = None
    size: tuple[float, float] | None = None
    scale: float = 1.0


@dataclass
class LogEntry:
    ts: float
    level: LogLevel
    message: str
    subsystem: str = ""
    process: str = ""


@dataclass
class CrashReport:
    ts: float
    process: str
    path: str
    summary: str = ""
    extra: dict[str, str] = field(default_factory=dict)


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


@runtime_checkable
class AppDriverV2(AppDriver, Protocol):
    def observe(self, name: str | None = None, *, screenshot: bool = True) -> ScreenObservation:
        """Capture the tree and (optionally) a screenshot in one round-trip.

        `name` names the screenshot file; drivers pick a unique name when it
        is None. Raise AppMissingError when the app is not launched and
        AppCrashedError when it has died.
        """

    def tap_point(self, x: float, y: float) -> None:
        """Tap or click at a point. Raise AppCrashedError if the app dies."""

    def swipe(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        duration_s: float = 0.3,
    ) -> None:
        """Drag from `start` to `end`. Scroll views move opposite to the drag."""

    def logs_since(self, ts: float) -> list[LogEntry]:
        """App log lines at or after `ts`, oldest first. Empty when unsupported."""

    def crash_reports_since(self, ts: float) -> list[CrashReport]:
        """Crash reports for the app written at or after `ts`, oldest first."""


_V2_METHODS = ("observe", "tap_point", "swipe", "logs_since", "crash_reports_since")


def supports_v2(driver: object) -> bool:
    """True when `driver` implements every AppDriverV2 method."""
    return all(callable(getattr(driver, name, None)) for name in _V2_METHODS)
