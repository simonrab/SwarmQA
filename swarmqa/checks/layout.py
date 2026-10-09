"""Layout checks from element frames and the screenshot.

Truncation, clipping off screen, overlap, small tap targets, missing
accessibility labels, and text contrast. Runs once per new screen
(`per_screen = True`). Frames are points; screenshot pixels are points times
`obs.scale`. Heuristic rules report advisory issues. See docs/checks.md for
every threshold and false-positive guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops, ImageOps, ImageStat

from swarmqa.checks._tree import (
    BAR_ROLES,
    FIELD_ROLES,
    INTERACTIVE_ROLES,
    MODAL_ROLES,
    SCROLL_ROLES,
    TEXT_ROLES,
    Frame,
    Node,
    area,
    contains,
    describe,
    intersection,
    nodes,
    query_for,
    screen_frame,
    stale_nodes,
    usable_frame,
)
from swarmqa.checks.protocol import CheckIssue, StepContext
from swarmqa.driver.protocol import ScreenObservation

Platform = Literal["ios", "macos"]

_ELLIPSIS = ("…", "...")
_PROGRESS_WORDS = frozenset(
    {
        "loading",
        "saving",
        "searching",
        "connecting",
        "updating",
        "syncing",
        "processing",
        "downloading",
        "uploading",
        "waiting",
        "preparing",
        "signing",
        "checking",
        "sending",
        "opening",
        "exporting",
        "importing",
        "more",
        "wait",
    }
)
_WORD = re.compile(r"[A-Za-z]+")
_TARGET_ROLES = frozenset({"button", "imagebutton", "link", "tab", "popupbutton"})
_LABEL_ROLES = (INTERACTIVE_ROLES - {"cell"}) | {"imagebutton"}
# Controls a person activates by their name. An identifier does not name
# them for VoiceOver, so these need a label (or a labelled child) of their own.
_NAMED_ROLES = frozenset({"button", "imagebutton", "link", "tab", "popupbutton"})
# Parts of a system control (a segment, a picker or menu item) whose hit
# area is the control's, not the part's reported frame.
_SYSTEM_PART_ROLES = frozenset(
    {"segmentedcontrol", "picker", "pickerwheel", "menu", "menubar", "menuitem"}
)


@dataclass
class LayoutSettings:
    """Tunables for `LayoutCheck`.

    Tap-target minimums are points per platform (Apple HIG 44 pt on iOS,
    WCAG 2.2 24 px on macOS). Contrast is estimated from the screenshot;
    the default minimum is 3:1 (WCAG AA for large text and for UI
    components), because iOS's own secondary label colour measures about
    3.3-4:1 on white and is not a defect. Set `contrast_min = 4.5` for strict
    WCAG AA on normal text. Text whose frame is at least `large_text_height`
    points tall counts as large. A ratio within `contrast_margin` of the
    minimum passes: the screenshot estimate is only that precise, and system
    button styles (a bordered destructive button measures about 2.9:1) sit
    right on the line.
    """

    platform: Platform = "ios"
    truncation: bool = True
    offscreen: bool = True
    overlap: bool = True
    tap_targets: bool = True
    missing_labels: bool = True
    contrast: bool = True
    min_target_ios: float = 44.0
    min_target_macos: float = 24.0
    overlap_ratio: float = 0.25
    offscreen_tolerance: float = 2.0
    min_font_pt: float = 11.0
    narrow_char_em: float = 0.4
    overflow_min_offset: float = 20.0
    contrast_min: float = 3.0
    contrast_large_min: float = 3.0
    large_text_height: float = 23.0
    contrast_margin: float = 0.2
    contrast_max_colors: int = 256
    contrast_min_background: float = 0.4
    contrast_min_foreground: float = 0.004
    max_elements: int = 300

    @property
    def min_target(self) -> float:
        return self.min_target_macos if self.platform == "macos" else self.min_target_ios


class LayoutCheck:
    name = "layout"
    per_screen = True

    def __init__(self, settings: LayoutSettings | None = None):
        self.settings = settings or LayoutSettings()
        self.last_error: str | None = None

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        self.last_error = None
        obs = ctx.after
        if ctx.crashed and not obs.tree:
            return []  # no screen after a crash
        all_nodes = nodes(obs.tree)
        screen = screen_frame(obs.tree, obs.size)
        stale = stale_nodes(all_nodes, screen)
        # `flat` keeps every node so ancestor indices resolve; the rules only
        # look at nodes on the page that is actually on screen.
        flat = all_nodes
        live = [node for node in all_nodes if node.index not in stale]
        settings = self.settings
        issues: list[CheckIssue] = []
        if settings.truncation:
            issues.extend(self._truncation(live))
        if settings.offscreen and obs.size is not None:
            issues.extend(self._offscreen(live, flat, obs.size))
        if settings.overlap:
            issues.extend(self._overlap(live, flat, screen))
        if settings.tap_targets:
            issues.extend(self._tap_targets(live, flat))
        if settings.missing_labels:
            issues.extend(self._missing_labels(live, flat))
        if settings.contrast and obs.screenshot is not None and not stale:
            # While a page slides in or out the screenshot shows it mid-move,
            # so its pixels do not line up with the frames.
            try:
                issues.extend(self._contrast(live, flat, obs, screen))
            except Exception as exc:  # unreadable screenshot: no contrast verdict
                self.last_error = f"contrast skipped: {exc}"
        for issue in issues:
            issue.check = self.name
            issue.screenshot = obs.screenshot
        return issues

    # Truncation ---------------------------------------------------------

    def _truncation(self, flat: list[Node]) -> list[CheckIssue]:
        issues: list[CheckIssue] = []
        for node in flat:
            element = node.element
            label = (element.label or "").strip()
            if node.role not in TEXT_ROLES or not label:
                continue
            frame = usable_frame(element)
            reason = _ellipsis_reason(label)
            if reason is None and frame is not None:
                reason = self._size_reason(label, frame)
            if reason is None:
                continue
            issues.append(
                CheckIssue(
                    kind="visual",
                    category="visual",
                    title=f"Text looks truncated: {_clip(label, 60)}",
                    severity="low",
                    confidence=0.6,
                    advisory=True,
                    details=reason,
                    element=query_for(element),
                    bbox=frame,
                )
            )
        return issues

    def _size_reason(self, label: str, frame: Frame) -> str | None:
        """Flag text that cannot fit even at the smallest font with narrow glyphs.

        Capacity assumes `min_font_pt` text, glyphs `narrow_char_em` wide,
        and as many lines of 1.2 x font height as the frame holds. That
        under-estimates the space real text needs, so only clear overflows
        are flagged.
        """
        settings = self.settings
        text = " ".join(label.split())
        if len(text) < 4:
            return None
        _, _, width, height = frame
        font = settings.min_font_pt
        lines = max(1, int(height // (font * 1.2)))
        capacity = (width / (font * settings.narrow_char_em)) * lines
        if len(text) <= capacity:
            return None
        return (
            f"{len(text)} characters in a {width:g}x{height:g} pt frame; at most "
            f"~{int(capacity)} fit even at {font:g} pt with narrow glyphs"
        )

    # Off screen ---------------------------------------------------------

    def _offscreen(self, live: list[Node], flat: list[Node], size: tuple[float, float]) -> list[CheckIssue]:
        screen: Frame = (0.0, 0.0, float(size[0]), float(size[1]))
        tolerance = self.settings.offscreen_tolerance
        padded: Frame = (-tolerance, -tolerance, screen[2] + 2 * tolerance, screen[3] + 2 * tolerance)
        issues: list[CheckIssue] = []
        for node in live:
            if node.role not in INTERACTIVE_ROLES:
                continue
            frame = usable_frame(node.element)
            if frame is None or contains(padded, frame, tolerance=0):
                continue
            if intersection(screen, frame) is None:
                continue  # fully off screen: paged, hidden, or scrolled away
            if _in_scroll(node, flat):
                continue  # partly scrolled out of view is normal
            window = _window_frame(node, flat)
            if window is not None and not contains(padded, window, tolerance=0):
                continue  # the window itself hangs off the display
            issues.append(
                CheckIssue(
                    kind="visual",
                    category="visual",
                    title=f"{_cap(describe(node.element))} is cut off at the screen edge",
                    severity="medium",
                    confidence=0.8,
                    details=(
                        f"frame {_fmt(frame)} extends past the {size[0]:g}x{size[1]:g} pt screen"
                    ),
                    element=query_for(node.element),
                    bbox=frame,
                )
            )
        return issues

    # Overlap ------------------------------------------------------------

    def _overlap(self, live: list[Node], flat: list[Node], screen: Frame | None) -> list[CheckIssue]:
        settings = self.settings
        candidates = [
            (node, frame)
            for node in live
            if node.role in INTERACTIVE_ROLES or (node.role in TEXT_ROLES and node.element.label)
            for frame in [usable_frame(node.element)]
            if frame is not None and (screen is None or intersection(screen, frame) is not None)
        ][: settings.max_elements]
        issues: list[CheckIssue] = []
        overflows: dict[int, tuple[Node, Frame, list[Node]]] = {}
        for i, (a, fa) in enumerate(candidates):
            for b, fb in candidates[i + 1 :]:
                if a.layer != b.layer:
                    continue  # different window or modal layer
                if a.index in b.ancestors or b.index in a.ancestors:
                    continue
                if contains(fa, fb) or contains(fb, fa):
                    continue  # containment: a label in a button, a badge on an icon
                shared = intersection(fa, fb)
                if shared is None or shared[2] < 2 or shared[3] < 2:
                    continue
                if area(shared) < settings.overlap_ratio * min(area(fa), area(fb)):
                    continue
                if _in_scroll(a, flat) != _in_scroll(b, flat):
                    continue  # content scrolled under fixed chrome
                overflow = self._overflow(a, fa, b, fb) or self._overflow(b, fb, a, fa)
                if overflow is not None:
                    text, text_frame, other = overflow
                    overflows.setdefault(text.index, (text, text_frame, []))[2].append(other)
                    continue
                both = a.role in INTERACTIVE_ROLES and b.role in INTERACTIVE_ROLES
                issues.append(
                    CheckIssue(
                        kind="visual",
                        category="visual",
                        title=f"{_cap(describe(a.element))} overlaps {describe(b.element)}",
                        severity="medium" if both else "low",
                        confidence=0.8 if both else 0.6,
                        advisory=not both,
                        details=f"frames {_fmt(fa)} and {_fmt(fb)} intersect in {_fmt(shared)}",
                        element=query_for(a.element),
                        bbox=shared,
                    )
                )
        for text, frame, others in overflows.values():
            names = ", ".join(describe(node.element) for node in others[:4])
            issues.append(
                CheckIssue(
                    kind="visual",
                    category="visual",
                    title=f"Text runs into the elements below it: {_clip(text.element.label, 60)}",
                    severity="medium",
                    confidence=0.7,
                    advisory=True,
                    details=(
                        f"The text frame {_fmt(frame)} extends under {names}. The text is taller "
                        "than the space it is drawn in, so it is probably clipped or drawn under "
                        "the next element."
                    ),
                    element=query_for(text.element),
                    bbox=frame,
                )
            )
        return issues

    def _overflow(self, text: Node, ft: Frame, other: Node, fo: Frame) -> tuple[Node, Frame, Node] | None:
        """`text` spills down into `other`, which starts well below the text's top.

        A layout stack places the next element under the text's frame. When
        the text's frame reaches into that next element, the text is taller
        than the space it was given (a fixed-height box, a clipped label).
        """
        if text.role not in TEXT_ROLES:
            return None
        if fo[1] - ft[1] < self.settings.overflow_min_offset:
            return None  # side by side or drawn on top, not stacked below
        if ft[3] <= fo[3] or ft[1] + ft[3] <= fo[1]:
            return None
        return text, ft, other

    # Tap targets --------------------------------------------------------

    def _tap_targets(self, live: list[Node], flat: list[Node]) -> list[CheckIssue]:
        minimum = self.settings.min_target
        issues: list[CheckIssue] = []
        for node in live:
            element = node.element
            if node.role not in _TARGET_ROLES or not element.enabled:
                continue
            frame = usable_frame(element)
            if frame is None:
                continue
            if frame[2] >= minimum - 0.5 and frame[3] >= minimum - 0.5:
                continue
            if node.role == "link" and _parent_role(node, flat) in TEXT_ROLES | FIELD_ROLES:
                continue  # inline link inside running text (WCAG exception)
            if any(flat[index].role in BAR_ROLES | _SYSTEM_PART_ROLES for index in node.ancestors):
                # System bar buttons and segments: UIKit pads their hit area
                # beyond the reported frame.
                continue
            if _in_large_cell(node, flat, minimum):
                continue  # the whole row is the target
            issues.append(
                CheckIssue(
                    kind="visual",
                    category="visual",
                    title=f"Small tap target: {describe(element)}",
                    severity="low",
                    confidence=0.7,
                    advisory=True,
                    details=(
                        f"{frame[2]:g}x{frame[3]:g} pt is under the {minimum:g}x{minimum:g} pt "
                        f"minimum for {self.settings.platform}. The hit area may be larger than "
                        "the accessibility frame."
                    ),
                    element=query_for(element),
                    bbox=frame,
                )
            )
        return issues

    # Missing labels -----------------------------------------------------

    def _missing_labels(self, live: list[Node], flat: list[Node]) -> list[CheckIssue]:
        issues: list[CheckIssue] = []
        for node in live:
            element = node.element
            if node.role not in _LABEL_ROLES:
                continue
            if (element.label or "").strip():
                continue
            named_by_id = bool((element.identifier or "").strip())
            if named_by_id and node.role not in _NAMED_ROLES:
                # Toggles and fields are usually named by a label element
                # next to them that the tree does not tie to the control.
                continue
            if element.frame is not None and usable_frame(element) is None:
                continue  # zero-size: not on screen
            if node.role in FIELD_ROLES and (element.value or "").strip():
                continue  # the value or placeholder names the field
            if _has_text_child(element):
                continue  # a child text element carries the name
            if _labelled_control_ancestor(node, flat):
                continue  # the inner part of a labelled control (a switch in its row)
            frame = usable_frame(element)
            if named_by_id:
                where = f' "{element.identifier}"'
            else:
                where = ""
                if frame is not None:
                    cx = round((frame[0] + frame[2] / 2) / 10) * 10
                    cy = round((frame[1] + frame[3] / 2) / 10) * 10
                    where = f" at ({cx}, {cy})"
            issues.append(
                CheckIssue(
                    kind="visual",
                    category="broken",
                    title=f"Unlabeled {node.role}{where}",
                    severity="medium",
                    details=(
                        "An interactive element has no accessibility label"
                        + (" (only an identifier, which VoiceOver does not read)" if named_by_id else "")
                        + ", so VoiceOver users cannot tell what it does."
                    ),
                    element=query_for(element),
                    bbox=frame,
                )
            )
        return issues

    # Contrast -----------------------------------------------------------

    def _contrast(
        self, live: list[Node], flat: list[Node], obs: ScreenObservation, screen: Frame | None
    ) -> list[CheckIssue]:
        settings = self.settings
        flat_roles = [node.role for node in flat]
        texts = [
            node
            for node in live
            if node.role in TEXT_ROLES
            and node.element.label
            and node.element.enabled
            # System alerts and action sheets draw on translucent material
            # over a dimmed screen; their styling is the system's, not the app's.
            and not any(flat_roles[index] in MODAL_ROLES for index in node.ancestors)
        ]
        if not texts:
            return []
        issues: list[CheckIssue] = []
        with Image.open(Path(obs.screenshot)) as raw:  # type: ignore[arg-type]
            image = raw.convert("RGB")
        try:
            for node in texts:
                frame = usable_frame(node.element)
                if frame is None:
                    continue
                if screen is not None and not contains(screen, frame):
                    continue  # partly off screen or scrolled away: the crop is not the text
                sample = sample_contrast(
                    image,
                    frame,
                    scale=obs.scale or 1.0,
                    max_colors=settings.contrast_max_colors,
                    min_background=settings.contrast_min_background,
                    min_foreground=settings.contrast_min_foreground,
                )
                if sample is None:
                    continue
                ratio, foreground, background = sample
                large = frame[3] >= settings.large_text_height
                needed = settings.contrast_large_min if large else settings.contrast_min
                if ratio >= needed - settings.contrast_margin:
                    continue  # within the estimate's error of passing
                issues.append(
                    CheckIssue(
                        kind="visual",
                        category="visual",
                        title=f"Low text contrast: {_clip(node.element.label, 60)}",
                        severity="medium",
                        confidence=0.6,
                        advisory=True,
                        details=(
                            f"estimated contrast {ratio:.2f}:1 between text {_hex(foreground)} and "
                            f"background {_hex(background)}; the check needs {needed:g}:1 for "
                            f"{'large' if large else 'normal'} text. Colours are sampled from the "
                            "screenshot."
                        ),
                        element=query_for(node.element),
                        bbox=frame,
                        extra={"contrast": f"{ratio:.2f}"},
                    )
                )
        finally:
            image.close()
        return issues


def relative_luminance(color: tuple[int, int, int]) -> float:
    """WCAG 2 relative luminance of an sRGB colour."""

    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = color[:3]
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    light, dark = max(la, lb), min(la, lb)
    return (light + 0.05) / (dark + 0.05)


def sample_contrast(
    image: Image.Image,
    frame: Frame,
    *,
    scale: float = 1.0,
    max_colors: int = 256,
    min_background: float = 0.4,
    min_foreground: float = 0.004,
) -> tuple[float, tuple[int, int, int], tuple[int, int, int]] | None:
    """Estimate text contrast inside `frame`, or None when the sample is ambiguous.

    Colours are posterised to 5 bits per channel. The most common colour is
    the background and must cover `min_background` of the crop; busy crops
    (photos, gradients: more than `max_colors` colours) are skipped. The
    foreground is the colour, covering at least `min_foreground`, with the
    highest contrast against the background, so anti-aliased edge pixels do
    not drag the estimate down.
    """
    x, y, width, height = frame
    left = max(0, int(round(x * scale)))
    top = max(0, int(round(y * scale)))
    right = min(image.width, int(round((x + width) * scale)))
    bottom = min(image.height, int(round((y + height) * scale)))
    if right - left < 3 or bottom - top < 3:
        return None
    crop = image.crop((left, top, right, bottom))
    if crop.width * crop.height > 40000:
        factor = (40000 / float(crop.width * crop.height)) ** 0.5
        crop = crop.resize(
            (max(3, int(crop.width * factor)), max(3, int(crop.height * factor))),
            Image.NEAREST,
        )
    original = crop.convert("RGB")
    crop = ImageOps.posterize(original, 5)
    colors = crop.getcolors(maxcolors=max_colors)
    if not colors or len(colors) < 2:
        return None
    total = float(crop.width * crop.height)
    colors.sort(key=lambda item: item[0], reverse=True)
    background_count, background = colors[0]
    if background_count / total < min_background:
        return None
    background_bucket = background
    background = _mid(background)
    best: tuple[float, tuple[int, int, int], tuple[int, int, int]] | None = None
    for count, color in colors[1:]:
        if count / total < min_foreground:
            continue
        ratio = contrast_ratio(_mid(color), background)
        if best is None or ratio > best[0]:
            best = (ratio, _mid(color), color)
    if best is None:
        return None
    # Re-read both colours as the mean of the real pixels in their buckets,
    # so the ratio is not off by the bucket width.
    fg = _bucket_mean(original, crop, best[2]) or best[1]
    bg = _bucket_mean(original, crop, background_bucket) or background
    return contrast_ratio(fg, bg), fg, bg


def _bucket_mean(original: Image.Image, posterized: Image.Image, bucket) -> tuple[int, int, int] | None:
    """Mean colour of the `original` pixels whose posterised colour is `bucket`."""
    mask = None
    for band, value in zip(posterized.split(), bucket[:3]):
        band_mask = band.point([255 if v == value else 0 for v in range(256)])
        mask = band_mask if mask is None else ImageChops.multiply(mask, band_mask)
    if mask is None or not mask.getbbox():
        return None
    mean = ImageStat.Stat(original, mask).mean
    return tuple(int(round(c)) for c in mean[:3])  # type: ignore[return-value]


def _mid(color) -> tuple[int, int, int]:
    """Undo posterise flooring by moving each channel to the middle of its bucket."""
    return tuple(min(255, int(c) | 0b100) if c else 0 for c in color[:3])  # type: ignore[return-value]


def _ellipsis_reason(label: str) -> str | None:
    """A label ending in an ellipsis mid-sentence, other than progress text ("Loading…")."""
    text = label.rstrip()
    ending = next((mark for mark in _ELLIPSIS if text.endswith(mark)), None)
    if ending is None:
        return None
    head = text[: -len(ending)].rstrip()
    words = _WORD.findall(head)
    if len(head) < 8 or len(words) < 2:
        return None
    if words[-1].lower() in _PROGRESS_WORDS or words[0].lower() in _PROGRESS_WORDS:
        return None
    return "the label ends with an ellipsis"


def _in_scroll(node: Node, flat: list[Node]) -> bool:
    return any(flat[index].role in SCROLL_ROLES for index in node.ancestors)


def _window_frame(node: Node, flat: list[Node]) -> Frame | None:
    for index in reversed(node.ancestors):
        if flat[index].role == "window":
            return usable_frame(flat[index].element)
    return None


def _parent_role(node: Node, flat: list[Node]) -> str:
    return flat[node.ancestors[-1]].role if node.ancestors else ""


def _in_large_cell(node: Node, flat: list[Node], minimum: float, slack: float = 4.0) -> bool:
    """Inside a list, table or menu cell about as tall as the minimum: the row is the target.

    `slack` allows the system's compact rows (a pull-down menu row on iPhone
    is 42 pt).
    """
    for index in reversed(node.ancestors):
        ancestor = flat[index]
        if ancestor.role == "cell":
            frame = usable_frame(ancestor.element)
            return frame is not None and frame[3] >= minimum - slack
    return False


def _labelled_control_ancestor(node: Node, flat: list[Node]) -> bool:
    """An interactive ancestor with a label names this element (a switch inside its labelled row)."""
    for index in node.ancestors:
        ancestor = flat[index]
        if ancestor.role in INTERACTIVE_ROLES and (ancestor.element.label or "").strip():
            return True
    return False


def _has_text_child(element) -> bool:
    stack = list(element.children)
    while stack:
        item = stack.pop()
        if (item.label or "").strip():
            return True
        stack.extend(item.children)
    return False


def _fmt(frame: Frame) -> str:
    return "(" + ", ".join(f"{value:g}" for value in frame) + ")"


def _hex(color: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02x}" for c in color)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _clip(text: str, limit: int) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 3].rstrip() + "..."
