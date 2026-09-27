from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from swarmqa.visual.baseline import update_baselines
from swarmqa.visual.diff import compare_screenshot


def _save(path: Path, size: tuple[int, int], color: tuple[int, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGBA" if len(color) == 4 else "RGB"
    Image.new(mode, size, color).save(path, format="PNG")


def _paint(path: Path, size: tuple[int, int], pixels: dict[tuple[int, int], tuple[int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, (20, 40, 60))
    for point, color in pixels.items():
        image.putpixel(point, color)
    image.save(path, format="PNG")


def test_identical_images_pass(tmp_path: Path):
    current = tmp_path / "current.png"
    baseline = tmp_path / "home.png"
    diff_out = tmp_path / "diffs" / "home.png"
    color = (20, 40, 60)
    _save(current, (12, 8), color)
    _save(baseline, (12, 8), color)
    before = baseline.read_bytes()

    result = compare_screenshot(current, baseline, diff_out, threshold=0.0)

    assert result.passed
    assert result.score == 0.0
    assert result.name == "home"
    assert result.diff_path == diff_out
    assert result.message == ""
    with Image.open(diff_out) as diff:
        assert diff.format == "PNG"
        assert diff.size == (12, 8)
        assert all(pixel[:3] == color for pixel in diff.get_flattened_data())
    assert baseline.read_bytes() == before


def test_recolored_image_fails_with_reviewable_diff(tmp_path: Path):
    current = tmp_path / "current.png"
    baseline = tmp_path / "home.png"
    diff_out = tmp_path / "diffs" / "home.png"
    _save(baseline, (16, 16), (20, 40, 60))
    _save(current, (16, 16), (0, 180, 40))
    before = baseline.read_bytes()

    result = compare_screenshot(current, baseline, diff_out, threshold=0.0)

    assert not result.passed
    assert result.score == 1.0
    assert result.diff_path == diff_out
    assert "pixels changed" in result.message
    with Image.open(diff_out) as diff:
        assert diff.format == "PNG"
        assert diff.size == (16, 16)
        pixels = list(diff.convert("RGB").get_flattened_data())
    assert pixels
    assert all(pixel[0] == 255 and pixel[1] < 40 and pixel[2] < 40 for pixel in pixels)
    assert baseline.read_bytes() == before
    assert not diff_out.samefile(baseline)


def test_threshold_can_be_loosened_to_pass(tmp_path: Path):
    current = tmp_path / "current.png"
    baseline = tmp_path / "panel.png"
    size = (8, 8)
    changed = {(x, 0): (220, 10, 10) for x in range(size[0])}
    changed.update({(x, 1): (220, 10, 10) for x in range(size[0])})
    _paint(baseline, size, {})
    _paint(current, size, changed)
    tight = compare_screenshot(
        current, baseline, tmp_path / "tight.png", threshold=0.24
    )
    loose = compare_screenshot(
        current, baseline, tmp_path / "loose.png", threshold=0.25
    )

    assert tight.score == 0.25
    assert not tight.passed
    assert loose.score == 0.25
    assert loose.passed


def test_channel_delta_above_16_counts(tmp_path: Path):
    baseline = tmp_path / "pixel.png"
    quiet = tmp_path / "quiet.png"
    loud = tmp_path / "loud.png"
    _save(baseline, (1, 1), (0, 0, 0))
    _save(quiet, (1, 1), (16, 0, 0))
    _save(loud, (1, 1), (17, 0, 0))

    within = compare_screenshot(quiet, baseline, tmp_path / "quiet-diff.png", threshold=0.0)
    beyond = compare_screenshot(loud, baseline, tmp_path / "loud-diff.png", threshold=0.0)

    assert within.passed
    assert within.score == 0.0
    assert not beyond.passed
    assert beyond.score == 1.0


def test_alpha_channel_delta_counts(tmp_path: Path):
    baseline = tmp_path / "pixel.png"
    current = tmp_path / "faded.png"
    _save(baseline, (1, 1), (10, 20, 30, 255))
    _save(current, (1, 1), (10, 20, 30, 238))

    result = compare_screenshot(current, baseline, tmp_path / "diff.png", threshold=0.0)

    assert not result.passed
    assert result.score == 1.0


def test_scales_current_to_baseline_size(tmp_path: Path):
    current = tmp_path / "current.png"
    baseline = tmp_path / "home.png"
    diff_out = tmp_path / "diff.png"
    color = (30, 60, 90)
    _save(baseline, (7, 2), color)
    _save(current, (3, 5), color)

    result = compare_screenshot(current, baseline, diff_out, threshold=0.0)

    assert result.passed
    assert result.score == 0.0
    with Image.open(diff_out) as diff:
        assert diff.size == (7, 2)


def test_missing_baseline_fails_without_writing(tmp_path: Path):
    current = tmp_path / "current.png"
    baseline = tmp_path / "baselines" / "home.png"
    diff_out = tmp_path / "diff.png"
    _save(current, (4, 4), (1, 2, 3))

    result = compare_screenshot(current, baseline, diff_out, threshold=0.01)

    assert not result.passed
    assert result.score == 1.0
    assert result.diff_path is None
    assert result.name == "home"
    assert "baseline is missing" in result.message
    assert not baseline.exists()
    assert not diff_out.exists()


def test_compare_does_not_modify_baseline_bytes(tmp_path: Path):
    current = tmp_path / "current.png"
    baseline = tmp_path / "baselines" / "home.png"
    _save(baseline, (6, 6), (8, 16, 24))
    _save(current, (6, 6), (240, 10, 10))
    before = baseline.read_bytes()

    written = compare_screenshot(
        current, baseline, tmp_path / "diff.png", threshold=0.0
    )
    refused = compare_screenshot(current, baseline, baseline, threshold=0.0)

    assert not written.passed
    assert written.diff_path == tmp_path / "diff.png"
    assert refused.diff_path is None
    assert "baseline path" in refused.message
    assert baseline.read_bytes() == before


def test_update_baselines_promotes_pngs(tmp_path: Path):
    source = tmp_path / "media"
    nested = source / "nested"
    nested.mkdir(parents=True)
    _save(source / "b.png", (2, 2), (1, 2, 3))
    _save(source / "a.png", (2, 2), (4, 5, 6))
    _save(nested / "hidden.png", (2, 2), (7, 8, 9))
    (source / "notes.txt").write_text("skip", encoding="utf-8")
    (source / "shot.PNG").write_bytes(b"not-a-real-png")
    baseline_dir = tmp_path / "baselines" / "v1"

    written = update_baselines(source, baseline_dir)

    assert written == [baseline_dir / "a.png", baseline_dir / "b.png"]
    assert (baseline_dir / "a.png").read_bytes() == (source / "a.png").read_bytes()
    assert (baseline_dir / "b.png").read_bytes() == (source / "b.png").read_bytes()
    assert not (baseline_dir / "notes.txt").exists()
    assert not (baseline_dir / "hidden.png").exists()
    assert not (baseline_dir / "shot.PNG").exists()

    _save(source / "a.png", (2, 2), (9, 9, 9))
    again = update_baselines(source, baseline_dir)
    assert again == written
    assert (baseline_dir / "a.png").read_bytes() == (source / "a.png").read_bytes()


def test_update_baselines_requires_source_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        update_baselines(tmp_path / "missing", tmp_path / "baselines")
    assert not (tmp_path / "baselines").exists()
