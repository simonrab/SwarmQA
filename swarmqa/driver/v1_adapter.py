"""Serve AppDriverV2 from a driver that only speaks v1.

`observe` reads the tree and takes a screenshot as two calls, so the two can
drift by one frame. `tap_point` clicks the smallest enabled element whose
frame holds the point. v1 drivers click by query, so it raises
UnsupportedAction when no element is there or when no query picks out that
exact element (for example an unlabeled control, or "Save" behind an earlier
"Save As…"). `swipe`
becomes a v1 `scroll` of `scroll_step` in the drag's vertical direction.
Logs and crash reports are empty. Every other attribute is the wrapped
driver's.
"""

from __future__ import annotations

import itertools
import time

from swarmqa.driver.protocol import (
    AppDriver,
    CrashReport,
    LogEntry,
    ScreenObservation,
    UnsupportedAction,
    supports_v2,
)
from swarmqa.driver.query import find_element, walk
from swarmqa.errors import ElementNotFoundError
from swarmqa.models import ElementQuery, UIElement


class V1Adapter:
    def __init__(self, driver: AppDriver, *, scroll_step: int = 3):
        self.driver = driver
        self.scroll_step = scroll_step
        self._names = itertools.count(1)

    def __getattr__(self, name: str):
        if name == "driver":
            raise AttributeError(name)
        return getattr(self.driver, name)

    def observe(self, name: str | None = None, *, screenshot: bool = True) -> ScreenObservation:
        ts = time.time()
        tree = self.driver.accessibility_tree()
        shot = None
        if screenshot:
            shot = self.driver.screenshot(name or f"observe-{next(self._names):04d}")
        return ScreenObservation(tree=tree, ts=ts, screenshot=shot)

    def tap_point(self, x: float, y: float) -> None:
        tree = self.driver.accessibility_tree()
        element = element_at(tree, x, y)
        if element is None:
            raise UnsupportedAction(f"no element at ({x:g}, {y:g}) for a v1 driver to click")
        query = ElementQuery(role=element.role, label=element.label or None, identifier=element.identifier)
        try:
            resolved = find_element(tree, query)
        except ElementNotFoundError:
            resolved = None
        if resolved is not element:
            raise UnsupportedAction(
                f"no query picks out the element at ({x:g}, {y:g}) for a v1 driver to click"
            )
        self.driver.click(query)

    def swipe(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        duration_s: float = 0.3,
    ) -> None:
        dy = end[1] - start[1]
        if dy == 0:
            raise UnsupportedAction("v1 drivers cannot swipe horizontally")
        # Dragging up moves content up, which is a scroll down.
        self.driver.scroll(self.scroll_step if dy < 0 else -self.scroll_step)

    def logs_since(self, ts: float) -> list[LogEntry]:
        return []

    def crash_reports_since(self, ts: float) -> list[CrashReport]:
        return []


def as_v2(driver: AppDriver):
    """Return `driver` if it already speaks v2, else wrap it in V1Adapter."""
    return driver if supports_v2(driver) else V1Adapter(driver)


def element_at(tree: list[UIElement], x: float, y: float) -> UIElement | None:
    """The smallest enabled element whose frame contains the point."""
    best: UIElement | None = None
    best_area = float("inf")
    for element in walk(tree):
        if not element.enabled or element.frame is None:
            continue
        left, top, width, height = element.frame
        if left <= x <= left + width and top <= y <= top + height:
            area = width * height
            if area < best_area:
                best, best_area = element, area
    return best
