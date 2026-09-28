"""In-process app double used by tests and by non-macOS campaign runs.

The fake records every action, serves a scripted accessibility tree, and
writes a real (tiny) PNG plus an optional video file so reporters can attach
paths without a display.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.driver.query import find_element
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
