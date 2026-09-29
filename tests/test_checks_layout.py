"""WP-B3 — layout checks: truncation, off-screen, overlap, tap targets, labels, contrast."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from swarmqa.checks import Check, LayoutCheck, LayoutSettings
from swarmqa.checks.layout import contrast_ratio, sample_contrast
from swarmqa.checks.protocol import StepContext
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.models import UIElement
from swarmqa.testing import make_app

ONLY = {
    "truncation": False,
    "offscreen": False,
    "overlap": False,
    "tap_targets": False,
    "missing_labels": False,
    "contrast": False,
}


def _settings(rule: str, **kwargs) -> LayoutSettings:
    flags = dict(ONLY)
    flags[rule] = True
    flags.update(kwargs)
    return LayoutSettings(**flags)


@pytest.fixture
def driver(tmp_path):
    return FakeDriver(make_app(tmp_path), tmp_path / "work")


def _run(driver, rule, children, *, shot=None, scale=1.0, size=(390, 844), extra_roots=(), **kwargs):
    tree = [UIElement(role="window", label="Main", frame=(0, 0, *size), children=list(children))]
    tree.extend(extra_roots)
    obs = ScreenObservation(tree=tree, ts=1.0, screenshot=shot, size=size, scale=scale)
    return LayoutCheck(_settings(rule, **kwargs)).run(StepContext(after=obs, driver=driver))


def test_layout_check_contract():
    check = LayoutCheck()
    assert isinstance(check, Check)
    assert check.per_screen is True


# Truncation -------------------------------------------------------------


def test_truncation_by_ellipsis(driver):
    text = UIElement(role="text", label="Your subscription renews on the fir…", frame=(20, 100, 300, 20))
    issues = _run(driver, "truncation", [text])
    assert len(issues) == 1
    assert issues[0].kind == "visual" and issues[0].category == "visual"
    assert issues[0].advisory and issues[0].bbox == (20.0, 100.0, 300.0, 20.0)


def test_progress_ellipsis_is_not_truncation(driver):
    texts = [
        UIElement(role="text", label="Loading…", frame=(20, 100, 300, 20)),
        UIElement(role="text", label="Saving your changes...", frame=(20, 130, 300, 20)),
        UIElement(role="button", label="Export…", frame=(20, 160, 100, 44)),
    ]
    assert _run(driver, "truncation", texts) == []


def test_truncation_by_frame_size(driver):
    label = "This label is far too long to fit in such a narrow frame"
    text = UIElement(role="text", label=label, frame=(20, 100, 80, 20))
    issues = _run(driver, "truncation", [text])
    assert len(issues) == 1 and "characters" in issues[0].details


def test_multiline_frame_fits(driver):
    label = "This label is long but wraps over several lines in a tall frame"
    text = UIElement(role="text", label=label, frame=(20, 100, 120, 80))
    assert _run(driver, "truncation", [text]) == []


# Off screen -------------------------------------------------------------


def test_button_cut_off_at_edge(driver):
    button = UIElement(role="button", label="Continue", frame=(330, 700, 120, 44))
    issues = _run(driver, "offscreen", [button])
    assert len(issues) == 1 and "cut off" in issues[0].title
    assert not issues[0].advisory


def test_offscreen_guards(driver):
    scrolled = UIElement(
        role="scrollarea",
        frame=(0, 0, 390, 844),
        children=[UIElement(role="button", label="Row 30", frame=(0, 820, 390, 44))],
    )
    fully_off = UIElement(role="button", label="Hidden page", frame=(400, 100, 120, 44))
    inside = UIElement(role="button", label="Ok", frame=(20, 100, 120, 44))
    assert _run(driver, "offscreen", [scrolled, fully_off, inside]) == []


def test_offscreen_skipped_when_window_hangs_off_display(driver):
    window = UIElement(
        role="window",
        label="Moved",
        frame=(300, 0, 400, 400),
        children=[UIElement(role="button", label="Close", frame=(600, 10, 60, 30))],
    )
    assert _run(driver, "offscreen", [], extra_roots=[window]) == []


# Overlap ----------------------------------------------------------------


def test_overlapping_buttons(driver):
    a = UIElement(role="button", label="Save", frame=(20, 100, 100, 44))
    b = UIElement(role="button", label="Cancel", frame=(60, 110, 100, 44))
    issues = _run(driver, "overlap", [a, b])
    assert len(issues) == 1
    issue = issues[0]
    assert "overlaps" in issue.title and not issue.advisory
    assert issue.bbox == (60.0, 110.0, 60.0, 34.0)


def test_overlap_guards(driver):
    button_with_label = UIElement(
        role="button",
        label="Save",
        frame=(20, 100, 100, 44),
        children=[UIElement(role="text", label="Save", frame=(30, 110, 80, 20))],
    )
    cell = UIElement(
        role="cell",
        label="Row",
        frame=(0, 200, 390, 44),
        children=[UIElement(role="checkbox", label="On", value="1", frame=(320, 206, 51, 31))],
    )
    badge = UIElement(role="text", label="3", frame=(30, 300, 10, 10))
    icon = UIElement(role="button", label="Inbox", frame=(20, 290, 44, 44))
    touching = UIElement(role="button", label="Next", frame=(120, 100, 100, 44))
    assert _run(driver, "overlap", [button_with_label, cell, badge, icon, touching]) == []


def test_overlap_ignores_other_layers_and_scrolled_content(driver):
    alert = UIElement(
        role="alert",
        label="Oops",
        frame=(50, 80, 290, 150),
        children=[UIElement(role="button", label="OK", frame=(60, 100, 100, 44))],
    )
    behind = UIElement(role="button", label="Save", frame=(40, 100, 100, 44))
    nav = UIElement(role="button", label="Back", frame=(0, 40, 80, 44))
    scroll = UIElement(
        role="scrollarea",
        frame=(0, 0, 390, 844),
        children=[UIElement(role="button", label="Row", frame=(0, 50, 390, 44))],
    )
    assert _run(driver, "overlap", [behind, nav, scroll], extra_roots=[alert]) == []


def test_overlap_with_text_is_advisory(driver):
    a = UIElement(role="text", label="Title", frame=(20, 100, 200, 30))
    b = UIElement(role="text", label="Subtitle", frame=(20, 110, 200, 30))
    issues = _run(driver, "overlap", [a, b])
    assert len(issues) == 1 and issues[0].advisory


# Tap targets ------------------------------------------------------------


def test_small_tap_target_ios(driver):
    small = UIElement(role="button", label="Info", frame=(20, 100, 24, 24))
    big = UIElement(role="button", label="Save", frame=(20, 200, 100, 44))
    issues = _run(driver, "tap_targets", [small, big])
    assert [i.element.label for i in issues] == ["Info"]
    assert issues[0].severity == "low"


def test_tap_target_threshold_macos(driver):
    button = UIElement(role="button", label="Info", frame=(20, 100, 30, 26))
    assert _run(driver, "tap_targets", [button], platform="macos") == []
    tiny = UIElement(role="button", label="x", frame=(20, 100, 16, 16))
    assert len(_run(driver, "tap_targets", [tiny], platform="macos")) == 1


def test_tap_target_guards(driver):
    disabled = UIElement(role="button", label="Info", enabled=False, frame=(20, 100, 20, 20))
    switch = UIElement(role="checkbox", label="On", frame=(20, 150, 51, 31))
    inline = UIElement(
        role="text",
        label="Read the terms",
        frame=(20, 200, 300, 20),
        children=[UIElement(role="link", label="terms", frame=(120, 200, 40, 18))],
    )
    assert _run(driver, "tap_targets", [disabled, switch, inline]) == []


# Missing labels ---------------------------------------------------------


def test_missing_label(driver):
    icon = UIElement(role="button", frame=(340, 50, 44, 44))
    issues = _run(driver, "missing_labels", [icon])
    assert len(issues) == 1
    assert issues[0].category == "broken"
    assert issues[0].title == "Unlabeled button at (360, 70)"


def test_missing_label_guards(driver):
    elements = [
        UIElement(role="button", identifier="close", frame=(0, 0, 44, 44)),
        UIElement(role="textfield", value="Search", frame=(0, 50, 200, 34)),
        UIElement(role="button", frame=(0, 100, 100, 44), children=[UIElement(role="text", label="Go")]),
        UIElement(role="button", frame=(0, 0, 0, 0)),
        UIElement(role="image", frame=(0, 200, 44, 44)),
        UIElement(role="text", frame=(0, 250, 44, 20)),
    ]
    assert _run(driver, "missing_labels", elements) == []


# Contrast ---------------------------------------------------------------


def _text_png(path, fg, bg, *, size=(200, 60), box=(10, 10, 190, 50)):
    image = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(image)
    # Fake glyph strokes: vertical bars so the text colour covers ~30% of the box.
    for x in range(box[0] + 4, box[2] - 4, 7):
        draw.rectangle((x, box[1] + 4, x + 2, box[3] - 4), fill=fg)
    image.save(path)
    return path


def test_contrast_ratio_known_values():
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert contrast_ratio((119, 119, 119), (255, 255, 255)) == pytest.approx(4.48, abs=0.01)


def test_low_contrast_text_is_flagged(driver, tmp_path):
    shot = _text_png(tmp_path / "low.png", fg=(200, 200, 200), bg=(255, 255, 255))
    text = UIElement(role="text", label="Faint caption", frame=(10, 10, 180, 18))
    issues = _run(driver, "contrast", [text], shot=shot)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.advisory and issue.kind == "visual"
    assert float(issue.extra["contrast"]) < 2.0
    assert issue.screenshot == shot


def test_high_contrast_text_passes(driver, tmp_path):
    shot = _text_png(tmp_path / "high.png", fg=(20, 20, 20), bg=(255, 255, 255))
    text = UIElement(role="text", label="Readable", frame=(10, 10, 180, 18))
    assert _run(driver, "contrast", [text], shot=shot) == []


def test_contrast_respects_scale(driver, tmp_path):
    # 2x screenshot: the text frame in points maps to pixels times two.
    shot = _text_png(tmp_path / "retina.png", fg=(200, 200, 200), bg=(255, 255, 255), size=(400, 120), box=(20, 20, 380, 100))
    text = UIElement(role="text", label="Faint", frame=(10, 10, 180, 18))
    assert len(_run(driver, "contrast", [text], shot=shot, scale=2.0)) == 1


def test_large_text_uses_lower_threshold(driver, tmp_path):
    # ~3.5:1 fails normal text but passes large text.
    shot = _text_png(tmp_path / "mid.png", fg=(130, 130, 130), bg=(255, 255, 255))
    small = UIElement(role="text", label="Small", frame=(10, 10, 180, 18))
    large = UIElement(role="text", label="Large", frame=(10, 10, 180, 40))
    assert len(_run(driver, "contrast", [small], shot=shot)) == 1
    assert _run(driver, "contrast", [large], shot=shot) == []


def test_contrast_skips_ambiguous_samples(tmp_path):
    uniform = Image.new("RGB", (100, 40), "white")
    assert sample_contrast(uniform, (0, 0, 100, 40)) is None
    noisy = Image.effect_noise((100, 40), 100).convert("RGB")
    assert sample_contrast(noisy, (0, 0, 100, 40)) is None
    assert sample_contrast(uniform, (500, 500, 10, 10)) is None


def test_contrast_skips_disabled_text(driver, tmp_path):
    shot = _text_png(tmp_path / "low.png", fg=(200, 200, 200), bg=(255, 255, 255))
    text = UIElement(role="text", label="Disabled", enabled=False, frame=(10, 10, 180, 18))
    assert _run(driver, "contrast", [text], shot=shot) == []


def test_unreadable_screenshot_fails_open(driver, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    text = UIElement(role="text", label="Hello", frame=(10, 10, 180, 18))
    check = LayoutCheck(_settings("contrast"))
    obs = ScreenObservation(tree=[text], ts=1.0, screenshot=bad, size=(390, 844))
    assert check.run(StepContext(after=obs, driver=driver)) == []
    assert "contrast skipped" in check.last_error


def test_default_settings_quiet_on_clean_screen(driver):
    tree = [
        UIElement(role="text", label="Settings", frame=(20, 60, 200, 30)),
        UIElement(role="button", label="Save", frame=(20, 100, 120, 44)),
        UIElement(role="textfield", label="Name", frame=(20, 160, 300, 44)),
    ]
    obs = ScreenObservation(tree=tree, ts=1.0, size=(390, 844))
    assert LayoutCheck().run(StepContext(after=obs, driver=driver)) == []
