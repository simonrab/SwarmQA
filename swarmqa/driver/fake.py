"""In-process app double used by tests and by non-macOS campaign runs.

The fake records every action, serves a scripted accessibility tree, and
writes a real (tiny) PNG plus an optional video file so reporters can attach
paths without a display. It implements AppDriverV2: `tap_point` clicks the
element under the point, and tests script logs and crash reports with
`add_log` and `add_crash`. `transitions` maps a clicked label to the tree
that replaces the current one, so multi-screen flows can be faked.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path

from swarmqa.driver.protocol import CrashReport, LogEntry, LogLevel, ScreenObservation
from swarmqa.driver.query import find_element
from swarmqa.driver.v1_adapter import element_at
from swarmqa.errors import AppCrashedError, AppMissingError, UITimeoutError
from swarmqa.models import AppTarget, BuildMetadata, ElementQuery, UIElement

# 1x1 RGB PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
)


class FakeDriver:
    def __init__(
        self,
        target: AppTarget,
        work_dir: Path,
        video_mode: str = "always",
        *,
        require_path: bool = True,
    ):
        self.target = target
        self.work_dir = Path(work_dir)
        self.video_mode = video_mode
        self.require_path = require_path
        self.tree: list[UIElement] = []
        self.actions_log: list[str] = []
        self.crash_labels: set[str] = set()
        self.timeout_labels: set[str] = set()
        self.launched = False
        self.closed = False
        self._video_on = False
        self.video_path: Path | None = None
        self.version = "1.0.0-fake"
        self.transitions: dict[str, list[UIElement]] = {}
        self.logs: list[LogEntry] = []
        self.crashes: list[CrashReport] = []
        self.screen_size: tuple[float, float] = (390.0, 844.0)
        self._observations = itertools.count(1)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def set_tree(self, elements: list[UIElement]) -> None:
        self.tree = elements

    def launch(self) -> None:
        self._require_app()
        self.launched = True
        self.closed = False
        self.actions_log.append("launch")

    def relaunch(self) -> None:
        self.close()
        self.launch()
        self.actions_log.append("relaunch")

    def close(self) -> None:
        if self._video_on:
            self.stop_video()
        self.launched = False
        self.closed = True

    def accessibility_tree(self) -> list[UIElement]:
        self._require_launched()
        return self.tree

    def click(self, target: ElementQuery) -> None:
        element = self._resolve(target)
        self._maybe_fail(element)
        self.actions_log.append(f"click:{element.label}")
        self._transition(element)

    def type_text(self, target: ElementQuery, text: str) -> None:
        element = self._resolve(target)
        self._maybe_fail(element)
        element.value = text
        self.actions_log.append(f"type:{element.label}:{text}")

    def keychord(self, keys: list[str]) -> None:
        self._require_launched()
        self.actions_log.append("key:" + "+".join(keys))

    def scroll(self, delta: int, target: ElementQuery | None = None) -> None:
        self._require_launched()
        if target is not None:
            self._resolve(target)
        self.actions_log.append(f"scroll:{delta}")

    def select_menu(self, path: list[str]) -> None:
        self._require_launched()
        label = " > ".join(path)
        query = ElementQuery(role="menu", label=path[-1] if path else None)
        try:
            element = find_element(self.tree, query)
            self._maybe_fail(element)
        except Exception:
            if path and path[-1] in self.crash_labels:
                raise AppCrashedError(path[-1])
        self.actions_log.append(f"menu:{label}")

    def wait_for(self, target: ElementQuery, timeout_s: float) -> UIElement:
        self._require_launched()
        if target.label and target.label in self.timeout_labels:
            raise UITimeoutError(f"timed out after {timeout_s}s waiting for {target.label}")
        return self._resolve(target)

    def screenshot(self, name: str) -> Path:
        self._require_launched()
        path = self.work_dir / "media" / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG)
        self.actions_log.append(f"screenshot:{name}")
        return path

    def start_video(self) -> None:
        self._require_launched()
        self._video_on = True
        self.video_path = self.work_dir / "media" / "session.mp4"
        self.video_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_path.write_bytes(b"fake-video")
        self.actions_log.append("video:start")

    def stop_video(self) -> Path | None:
        self._video_on = False
        self.actions_log.append("video:stop")
        return self.video_path

    def observe(self, name: str | None = None, *, screenshot: bool = True) -> ScreenObservation:
        self._require_launched()
        shot = None
        if screenshot:
            shot = self.screenshot(name or f"observe-{next(self._observations):04d}")
        return ScreenObservation(
            tree=self.tree,
            ts=time.time(),
            screenshot=shot,
            size=self.screen_size,
        )

    def tap_point(self, x: float, y: float) -> None:
        self._require_launched()
        element = element_at(self.tree, x, y)
        self.actions_log.append(f"tap:{x:g},{y:g}")
        if element is not None:
            self._maybe_fail(element)
            self._transition(element)

    def swipe(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        duration_s: float = 0.3,
    ) -> None:
        self._require_launched()
        self.actions_log.append(f"swipe:{start[0]:g},{start[1]:g}->{end[0]:g},{end[1]:g}")

    def logs_since(self, ts: float) -> list[LogEntry]:
        return [entry for entry in self.logs if entry.ts >= ts]

    def crash_reports_since(self, ts: float) -> list[CrashReport]:
        return [report for report in self.crashes if report.ts >= ts]

    def add_log(self, message: str, level: LogLevel = "info", *, ts: float | None = None) -> None:
        self.logs.append(LogEntry(ts=time.time() if ts is None else ts, level=level, message=message))

    def add_crash(self, summary: str = "", *, ts: float | None = None) -> CrashReport:
        report = CrashReport(
            ts=time.time() if ts is None else ts,
            process=self.target.bundle_id or "FakeApp",
            path=str(self.work_dir / "crashes" / f"crash-{len(self.crashes) + 1}.ips"),
            summary=summary,
        )
        self.crashes.append(report)
        return report

    def metadata(self) -> BuildMetadata:
        return BuildMetadata(
            path=self.target.path,
            bundle_id=self.target.bundle_id,
            version=self.version,
            backend="fake",
        )

    def _resolve(self, target: ElementQuery) -> UIElement:
        self._require_launched()
        return find_element(self.tree, target)

    def _transition(self, element: UIElement) -> None:
        if element.label in self.transitions:
            self.tree = self.transitions[element.label]

    def _maybe_fail(self, element: UIElement) -> None:
        if element.label in self.crash_labels:
            raise AppCrashedError(f"crash on {element.label}")
        if element.label in self.timeout_labels:
            raise UITimeoutError(f"unresponsive control {element.label}")

    def _require_launched(self) -> None:
        if not self.launched:
            raise AppMissingError("app is not launched")

    def _require_app(self) -> None:
        if self.target.build_command and not self.target.path:
            return
        if not self.target.path:
            raise AppMissingError("app path is missing")
        if self.require_path and not Path(self.target.path).exists():
            raise AppMissingError(f"app not found: {self.target.path}")
