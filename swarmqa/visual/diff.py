"""C6 — compare a screenshot to a baseline.

See docs/CONTRACTS.md section C6 and docs/visual.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# A pixel counts as changed when any RGBA channel moves by more than this.
CHANNEL_DELTA = 16

_RESAMPLE = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


@dataclass
class DiffResult:
    name: str
    passed: bool
    score: float
    diff_path: Path | None
    message: str = ""


def compare_screenshot(
    current: Path,
    baseline: Path,
    diff_out: Path,
    *,
    threshold: float,
) -> DiffResult:
    """Compare images. Write a highlighted diff. Never modify `baseline`."""
    current_path = Path(current)
    baseline_path = Path(baseline)
    diff_path = Path(diff_out)
    name = baseline_path.stem

    if not baseline_path.is_file():
        return DiffResult(
            name=name,
            passed=False,
            score=1.0,
            diff_path=None,
            message="baseline is missing",
        )
    if not current_path.is_file():
        return DiffResult(
            name=name,
            passed=False,
            score=1.0,
            diff_path=None,
            message="current screenshot is missing",
        )

    baseline_image = _load_rgba(baseline_path)
    current_image = _load_rgba(current_path)
    try:
        score, highlight = _score_and_highlight(baseline_image, current_image)
    finally:
        baseline_image.close()
        current_image.close()

    passed = score <= threshold
    detail = "" if passed else f"{score:.6f} of pixels changed (threshold {threshold})"
    if _same_path(diff_path, baseline_path):
        message = "refused to write diff into the baseline path"
        if detail:
            message = f"{message}; {detail}"
        return DiffResult(
            name=name,
            passed=passed,
            score=score,
            diff_path=None,
            message=message,
        )

    diff_path.parent.mkdir(parents=True, exist_ok=True)
    highlight.save(diff_path, format="PNG")
    highlight.close()
    return DiffResult(
        name=name,
        passed=passed,
        score=score,
        diff_path=diff_path,
        message=detail,
    )


def _load_rgba(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA").copy()


def _score_and_highlight(
    baseline: Image.Image,
    current: Image.Image,
) -> tuple[float, Image.Image]:
    if current.size != baseline.size:
        current = current.resize(baseline.size, _RESAMPLE)
    width, height = baseline.size
    total = width * height
    if total == 0:
        return 0.0, Image.new("RGB", (width, height))

    base_bytes = baseline.tobytes()
    curr_bytes = current.tobytes()
    highlight = bytearray(total * 3)
    changed = 0
    for index in range(total):
        offset = index * 4
        dest = index * 3
        if _channel_changed(base_bytes, curr_bytes, offset):
            changed += 1
            # Bright red, with a trace of the current pixel so the region stays readable.
            highlight[dest] = 255
            highlight[dest + 1] = curr_bytes[offset + 1] // 8
            highlight[dest + 2] = curr_bytes[offset + 2] // 8
        else:
            highlight[dest] = curr_bytes[offset]
            highlight[dest + 1] = curr_bytes[offset + 1]
            highlight[dest + 2] = curr_bytes[offset + 2]
    return changed / total, Image.frombytes("RGB", (width, height), bytes(highlight))


def _channel_changed(baseline: bytes, current: bytes, offset: int) -> bool:
    return (
        abs(baseline[offset] - current[offset]) > CHANNEL_DELTA
        or abs(baseline[offset + 1] - current[offset + 1]) > CHANNEL_DELTA
        or abs(baseline[offset + 2] - current[offset + 2]) > CHANNEL_DELTA
        or abs(baseline[offset + 3] - current[offset + 3]) > CHANNEL_DELTA
    )


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()
