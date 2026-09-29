"""WP-B3 — baseline check: vectorised diff, threshold, size mismatch, missing baseline."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from swarmqa.checks import BaselineCheck, BaselineSettings, Check
from swarmqa.checks.baseline import baseline_key, diff_files, diff_images, highlight
from swarmqa.checks.protocol import StepContext
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.testing import make_app


@pytest.fixture
def driver(tmp_path):
    return FakeDriver(make_app(tmp_path), tmp_path / "work")


def _png(path, size=(100, 100), color="white", box=None):
    image = Image.new("RGB", size, color)
    if box is not None:
        ImageDraw.Draw(image).rectangle(box, fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _ctx(driver, shot, screen_id="home"):
    return StepContext(after=ScreenObservation(tree=[], ts=1.0, screenshot=shot), driver=driver, screen_id=screen_id)


def _settings(tmp_path, **kwargs):
    return BaselineSettings(baseline_dir=str(tmp_path / "baselines"), **kwargs)


def test_baseline_check_contract(tmp_path):
    check = BaselineCheck(_settings(tmp_path))
    assert isinstance(check, Check) and check.per_screen


def test_identical_passes(driver, tmp_path):
    _png(tmp_path / "baselines" / "home.png")
    shot = _png(tmp_path / "shots" / "now.png")
    assert BaselineCheck(_settings(tmp_path)).run(_ctx(driver, shot)) == []


def test_change_above_threshold(driver, tmp_path):
    _png(tmp_path / "baselines" / "home.png")
    shot = _png(tmp_path / "shots" / "now.png", box=(0, 0, 19, 9))  # 200 px = 2%
    check = BaselineCheck(_settings(tmp_path, threshold=0.01, diff_dir=str(tmp_path / "diffs")))
    issues = check.run(_ctx(driver, shot))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "visual" and issue.category == "visual" and not issue.advisory
    assert issue.extra["score"] == "0.020000"
    diff = tmp_path / "diffs" / "home-diff.png"
    assert issue.extra["diff"] == str(diff) and diff.is_file()
    with Image.open(diff) as image:
        assert image.getpixel((5, 5))[0] == 255
        assert image.getpixel((50, 50)) == (255, 255, 255)
    assert issue.screenshot == shot


def test_change_below_threshold(driver, tmp_path):
    _png(tmp_path / "baselines" / "home.png")
    shot = _png(tmp_path / "shots" / "now.png", box=(0, 0, 9, 4))  # 50 px = 0.5%
    assert BaselineCheck(_settings(tmp_path, threshold=0.01)).run(_ctx(driver, shot)) == []


def test_size_mismatch_is_its_own_issue(driver, tmp_path):
    _png(tmp_path / "baselines" / "home.png", size=(100, 100))
    shot = _png(tmp_path / "shots" / "now.png", size=(200, 200))
    issues = BaselineCheck(_settings(tmp_path)).run(_ctx(driver, shot))
    assert len(issues) == 1
    assert "size differs" in issues[0].title
    assert "100x100" in issues[0].details and "200x200" in issues[0].details


def test_missing_baseline_reports_advisory(driver, tmp_path):
    shot = _png(tmp_path / "shots" / "now.png")
    issues = BaselineCheck(_settings(tmp_path)).run(_ctx(driver, shot))
    assert len(issues) == 1
    assert "baseline is missing" in issues[0].details
    assert issues[0].advisory and issues[0].severity == "low"
    assert not (tmp_path / "baselines").exists()


def test_missing_baseline_record_writes_candidate_not_baseline(driver, tmp_path):
    shot = _png(tmp_path / "shots" / "now.png")
    settings = _settings(tmp_path, on_missing="record", record_dir=str(tmp_path / "candidates"))
    issues = BaselineCheck(settings).run(_ctx(driver, shot, screen_id="screen/1 two"))
    key = baseline_key("screen/1 two")
    assert key == "screen-1-two"
    assert (tmp_path / "candidates" / f"{key}.png").is_file()
    assert not (tmp_path / "baselines").exists()
    assert issues[0].extra["recorded"].endswith(f"{key}.png")


def test_missing_baseline_ignore(driver, tmp_path):
    shot = _png(tmp_path / "shots" / "now.png")
    assert BaselineCheck(_settings(tmp_path, on_missing="ignore")).run(_ctx(driver, shot)) == []


def test_named_key_overrides_screen_id(driver, tmp_path):
    _png(tmp_path / "baselines" / "login.png")
    shot = _png(tmp_path / "shots" / "now.png")
    assert BaselineCheck(_settings(tmp_path), key="login").run(_ctx(driver, shot, screen_id="abc")) == []


def test_no_screenshot_no_issue(driver, tmp_path):
    assert BaselineCheck(_settings(tmp_path)).run(_ctx(driver, None)) == []


def test_diff_images_vectorised_matches_manual_count():
    base = Image.new("RGBA", (50, 40), (10, 10, 10, 255))
    curr = base.copy()
    ImageDraw.Draw(curr).rectangle((0, 0, 9, 9), fill=(10, 10, 27, 255))  # delta 17 > 16
    ImageDraw.Draw(curr).rectangle((20, 20, 29, 29), fill=(10, 26, 10, 255))  # delta 16: unchanged
    result = diff_images(base, curr)
    assert result.changed == 100 and result.total == 2000
    assert result.score == pytest.approx(0.05)
    marked = highlight(curr, result.mask)
    assert marked.getpixel((0, 0)) == (255, 10 // 8, 27 // 8)
    assert marked.getpixel((25, 25)) == (10, 26, 10)
    assert diff_images(base, Image.new("RGBA", (5, 5))) is None


def test_diff_files_size_mismatch(tmp_path):
    a = _png(tmp_path / "a.png", size=(10, 10))
    b = _png(tmp_path / "b.png", size=(12, 10))
    assert diff_files(a, b) is None
    assert diff_files(a, a) == 0.0
