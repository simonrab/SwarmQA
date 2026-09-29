"""Screenshot baseline check, and the vectorised pixel diff it shares with `visual/diff.py`.

A pixel counts as changed when any RGBA channel moves by more than
`CHANNEL_DELTA`. The diff runs in Pillow (`ImageChops`, `point`,
`histogram`), with no per-pixel Python loop. Images of different sizes are
never resized here: a size mismatch is its own issue.

Baselines live in `baseline_dir` as `<key>.png`, where the key is the name
the check was built with or else the agent loop's screen id. This check
never writes into `baseline_dir`; `visual.baseline.update_baselines` stays
the only write path. With `on_missing="record"` it copies the current
screenshot into `record_dir` so it can be promoted later.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops

from swarmqa.checks.protocol import CheckIssue, StepContext

CHANNEL_DELTA = 16

OnMissing = Literal["report", "record", "ignore"]
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class BaselineSettings:
    """Tunables for `BaselineCheck`.

    `threshold` is the largest changed-pixel fraction (0-1) that still
    passes. `diff_dir` receives `<key>-diff.png` highlights when set.
    `on_missing`: `report` raises an advisory issue, `record` also copies
    the screenshot to `record_dir` (for `aqa baseline update --from`),
    `ignore` says nothing.
    """

    baseline_dir: str = "baselines"
    threshold: float = 0.01
    channel_delta: int = CHANNEL_DELTA
    diff_dir: str | None = None
    on_missing: OnMissing = "report"
    record_dir: str | None = None


@dataclass
class ImageDiff:
    score: float
    changed: int
    total: int
    mask: Image.Image


def load_rgba(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA").copy()


def diff_images(
    baseline: Image.Image,
    current: Image.Image,
    *,
    channel_delta: int = CHANNEL_DELTA,
) -> ImageDiff | None:
    """Changed-pixel fraction and an `L` mask (255 = changed). None when sizes differ."""
    if baseline.size != current.size:
        return None
    base = baseline.convert("RGBA") if baseline.mode != "RGBA" else baseline
    curr = current.convert("RGBA") if current.mode != "RGBA" else current
    width, height = base.size
    total = width * height
    if total == 0:
        return ImageDiff(score=0.0, changed=0, total=0, mask=Image.new("L", (width, height)))
    delta = ImageChops.difference(base, curr)
    table = [255 if value > channel_delta else 0 for value in range(256)]
    channels = [band.point(table) for band in delta.split()]
    mask = channels[0]
    for band in channels[1:]:
        mask = ImageChops.lighter(mask, band)
    changed = mask.histogram()[255]
    return ImageDiff(score=changed / total, changed=changed, total=total, mask=mask)


def highlight(current: Image.Image, mask: Image.Image) -> Image.Image:
    """RGB copy of `current` with changed pixels bright red (keeping a trace of the pixel)."""
    rgb = current.convert("RGB")
    _, green, blue = rgb.split()
    eighth = [value // 8 for value in range(256)]
    red = Image.merge(
        "RGB",
        (Image.new("L", rgb.size, 255), green.point(eighth), blue.point(eighth)),
    )
    return Image.composite(red, rgb, mask)


def diff_files(baseline: Path, current: Path, *, channel_delta: int = CHANNEL_DELTA) -> float | None:
    """Changed-pixel fraction between two PNG files; None when sizes differ."""
    left = load_rgba(Path(baseline))
    right = load_rgba(Path(current))
    try:
        result = diff_images(left, right, channel_delta=channel_delta)
    finally:
        left.close()
        right.close()
    return None if result is None else result.score


def baseline_key(name: str) -> str:
    """File-safe baseline key (`<key>.png`)."""
    cleaned = _SAFE.sub("-", name.strip()).strip("-.")
    return cleaned[:120] or "screen"


class BaselineCheck:
    name = "baseline"
    per_screen = True

    def __init__(self, settings: BaselineSettings | None = None, *, key: str | None = None):
        self.settings = settings or BaselineSettings()
        self.key = key
        self.last_error: str | None = None

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        self.last_error = None
        shot = ctx.after.screenshot
        name = self.key or ctx.screen_id
        if shot is None or not name:
            return []
        current_path = Path(shot)
        if not current_path.is_file():
            self.last_error = f"screenshot is missing: {current_path}"
            return []
        settings = self.settings
        key = baseline_key(name)
        baseline_path = Path(settings.baseline_dir) / f"{key}.png"
        if not baseline_path.is_file():
            return self._missing(key, baseline_path, current_path)

        base = load_rgba(baseline_path)
        curr = load_rgba(current_path)
        try:
            if base.size != curr.size:
                return [
                    self._issue(
                        title=f"Screenshot size differs from baseline: {key}",
                        details=(
                            f"baseline {base.size[0]}x{base.size[1]} px, current "
                            f"{curr.size[0]}x{curr.size[1]} px ({baseline_path}). Not resized: "
                            "check the device, scale, or window size, then re-record."
                        ),
                        current=current_path,
                        extra={"baseline": str(baseline_path)},
                    )
                ]
            result = diff_images(base, curr, channel_delta=settings.channel_delta)
            assert result is not None
            if result.score <= settings.threshold:
                return []
            extra = {"baseline": str(baseline_path), "score": f"{result.score:.6f}"}
            diff_path = self._write_diff(key, curr, result.mask, baseline_path)
            if diff_path is not None:
                extra["diff"] = str(diff_path)
        finally:
            base.close()
            curr.close()
        return [
            self._issue(
                title=f"Screen differs from baseline: {key}",
                details=(
                    f"{result.score:.6f} of pixels changed (threshold {settings.threshold}); "
                    f"baseline {baseline_path}"
                    + (f"; diff {extra['diff']}" if "diff" in extra else "")
                ),
                current=current_path,
                extra=extra,
            )
        ]

    def _missing(self, key: str, baseline_path: Path, current: Path) -> list[CheckIssue]:
        settings = self.settings
        if settings.on_missing == "ignore":
            return []
        details = f"baseline is missing: {baseline_path}"
        extra = {"baseline": str(baseline_path)}
        if settings.on_missing == "record" and settings.record_dir:
            record = Path(settings.record_dir)
            target = record / f"{key}.png"
            if _same(target, baseline_path):
                self.last_error = "refused to record into the baseline directory"
            else:
                record.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(current, target)
                extra["recorded"] = str(target)
                details += f"; recorded candidate {target} (promote with `aqa baseline update`)"
        return [
            self._issue(
                title=f"No baseline for screen: {key}",
                details=details,
                current=current,
                extra=extra,
                severity="low",
                advisory=True,
            )
        ]

    def _write_diff(self, key: str, current: Image.Image, mask: Image.Image, baseline: Path) -> Path | None:
        if not self.settings.diff_dir:
            return None
        path = Path(self.settings.diff_dir) / f"{key}-diff.png"
        if _same(path, baseline):
            self.last_error = "refused to write diff into the baseline path"
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        image = highlight(current, mask)
        image.save(path, format="PNG")
        image.close()
        return path

    def _issue(
        self,
        *,
        title: str,
        details: str,
        current: Path,
        extra: dict[str, str],
        severity: str = "medium",
        advisory: bool = False,
    ) -> CheckIssue:
        return CheckIssue(
            kind="visual",
            category="visual",
            title=title,
            severity=severity,  # type: ignore[arg-type]
            advisory=advisory,
            details=details,
            screenshot=current,
            check=self.name,
            extra=extra,
        )


def _same(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()
