"""Checks against real runner observations of the PlantedBugs fixture (iPhone 17, iOS 26.5).

The fixtures in `tests/fixtures/checks/` are `/observe` responses (tree,
size, scale) with their screenshots, captured from the swarm runner. They
cover the false positives seen in live runs (a stale NavigationStack page in
the tree, system secondary text, bar buttons, inner switches, iOS log noise)
and the planted bugs the checks must still catch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarmqa.checks import FunctionalCheck, FunctionalSettings, LayoutCheck
from swarmqa.checks._tree import nodes, screen_frame, stale_nodes
from swarmqa.checks.protocol import StepContext
from swarmqa.driver.protocol import LogEntry, ScreenObservation
from swarmqa.driver.runner_schema import decode_element
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import ElementQuery, UIElement

FIXTURES = Path(__file__).parent / "fixtures" / "checks"


def load(name: str, *, screenshot: bool = True) -> ScreenObservation:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    shot = None
    if screenshot:
        for suffix in (".png", ".jpg"):
            if (FIXTURES / f"{name}{suffix}").is_file():
                shot = FIXTURES / f"{name}{suffix}"
    return ScreenObservation(
        tree=[decode_element(e) for e in data["elements"]],
        ts=data["ts"],
        screenshot=shot,
        size=tuple(data["size"]),
        scale=data["scale"],
    )


class StubDriver:
    """Just enough driver for the checks: canned logs, crash reports and observations."""

    bundle_id = "dev.swarmqa.PlantedBugs"

    def __init__(self, logs=(), observations=()):
        self.logs = list(logs)
        self.observations = list(observations)
        self.observe_calls = 0

    def logs_since(self, ts):
        return list(self.logs)

    def crash_reports_since(self, ts):
        return []

    def observe(self, name=None, *, screenshot=True):
        self.observe_calls += 1
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]


def layout(name: str, **kwargs):
    obs = load(name, **kwargs)
    return LayoutCheck().run(StepContext(after=obs, driver=StubDriver()))


def ids(issues):
    return {issue.element.identifier for issue in issues if issue.element is not None}


# Stale navigation page ------------------------------------------------------


def test_push_transition_marks_previous_page_stale():
    obs = load("push_report_0", screenshot=False)
    flat = nodes(obs.tree)
    stale = stale_nodes(flat, screen_frame(obs.tree, obs.size))
    stale_ids = {flat[i].element.identifier for i in stale}
    live_ids = {n.element.identifier for n in flat if n.index not in stale}
    assert "home.row.report" in stale_ids and "home.tipLabel" in stale_ids
    assert {"detail.title", "detail.priorityBadge", "detail.shareButton", "BackButton"} <= live_ids


def test_settled_screens_have_no_stale_nodes():
    for name in ("home", "detail_report", "settings", "add_sheet"):
        obs = load(name, screenshot=False)
        flat = nodes(obs.tree)
        assert stale_nodes(flat, screen_frame(obs.tree, obs.size)) == set(), name


def test_stale_home_rows_do_not_overlap_the_detail_page():
    issues = layout("push_report_0")
    for issue in issues:
        text = issue.title + " " + (issue.element.identifier or "" if issue.element else "")
        assert "home." not in text and "Water the plants" not in text, issue.title
    assert not [i for i in issues if not i.advisory]
    # The screenshot is mid-slide, so contrast is not judged at all.
    assert not [i for i in issues if i.title.startswith("Low text contrast")]


# Task detail: planted overlap and clipped notes -----------------------------


def test_detail_catches_badge_overlap_and_clipped_notes():
    issues = layout("detail_report")
    overlap = [i for i in issues if "overlaps" in i.title]
    assert [i.element.identifier for i in overlap] == ["detail.priorityBadge"]
    overflow = [i for i in issues if i.title.startswith("Text runs into")]
    assert [i.element.identifier for i in overflow] == ["detail.notes"]
    assert overflow[0].category == "visual"
    # Nothing on this screen is a confident (non-advisory) problem.
    assert [i.title for i in issues if not i.advisory] == []


def test_labelled_toggle_inner_switch_is_not_unlabeled():
    assert not [i for i in layout("detail_report") if i.title.startswith("Unlabeled")]


# Home: missing label and low contrast ---------------------------------------


def test_home_flags_unlabeled_filter_button():
    issues = [i for i in layout("home") if i.title.startswith("Unlabeled")]
    assert len(issues) == 1
    issue = issues[0]
    assert issue.element.identifier == "home.filterButton"
    assert issue.category == "broken" and not issue.advisory


def test_home_contrast_flags_only_the_tip():
    issues = [i for i in layout("home") if i.title.startswith("Low text contrast")]
    assert ids(issues) == {"home.tipLabel"}
    assert float(issues[0].extra["contrast"]) < 2.0


def test_home_bar_buttons_are_not_small_targets():
    assert not [i for i in layout("home") if i.title.startswith("Small tap target")]


@pytest.mark.parametrize("name", ["settings", "add_sheet", "about", "picker_open", "delete_dialog"])
def test_clean_screens_are_quiet(name):
    # Grey section headers and secondary text, form pickers, nav bar Cancel/Save,
    # pull-down menu rows (42 pt) and a system confirmation dialog.
    assert [i.title for i in layout(name)] == []


# Dead button -----------------------------------------------------------------


def test_share_button_that_does_nothing_is_a_dead_tap():
    before = load("detail_report", screenshot=False)
    after = load("detail_report_after_share", screenshot=False)
    action = StepDecision(kind="tap", target=ElementQuery(identifier="detail.shareButton"))
    ctx = StepContext(after=after, before=before, driver=StubDriver(), action=action, since_ts=after.ts - 1)
    issues = FunctionalCheck(FunctionalSettings(endless_spinners=False)).run(ctx)
    dead = [i for i in issues if i.kind == "unresponsive"]
    assert len(dead) == 1 and dead[0].element.identifier == "detail.shareButton"
    assert dead[0].category == "broken" and not dead[0].advisory


def test_tap_that_navigates_is_not_dead():
    before = load("home", screenshot=False)
    after = load("detail_report", screenshot=False)
    action = StepDecision(kind="tap", target=ElementQuery(identifier="home.row.report"))
    ctx = StepContext(after=after, before=before, driver=StubDriver(), action=action, since_ts=after.ts - 1)
    assert FunctionalCheck().run(ctx) == []


# Endless spinner -------------------------------------------------------------


def _spinner_check(**kwargs) -> FunctionalCheck:
    settings = dict(spinner_timeout_s=0.3, spinner_poll_s=0.05, console_errors=False)
    settings.update(kwargs)
    return FunctionalCheck(FunctionalSettings(**settings))


def test_spinner_that_never_stops_is_reported():
    stats = load("stats_6", screenshot=False)
    driver = StubDriver(observations=[stats])
    check = _spinner_check()
    issues = check.run(StepContext(after=stats, driver=driver, since_ts=stats.ts - 1))
    assert [(i.kind, i.category) for i in issues] == [("timeout", "broken")]
    assert issues[0].element.identifier == "stats.loadingSpinner"
    assert driver.observe_calls >= 1
    # Reported once per session.
    assert check.run(StepContext(after=stats, driver=driver, since_ts=stats.ts)) == []


def test_spinner_that_finishes_is_not_reported():
    stats = load("stats_0", screenshot=False)
    done = ScreenObservation(
        tree=[UIElement(role="application", frame=(0, 0, 402, 874), children=[
            UIElement(role="text", label="12 tasks done", identifier="stats.summaryLabel", frame=(100, 480, 200, 30))
        ])],
        ts=stats.ts + 1,
        size=stats.size,
    )
    driver = StubDriver(observations=[done])
    assert _spinner_check().run(StepContext(after=stats, driver=driver, since_ts=stats.ts - 1)) == []


def test_spinner_without_waiting_uses_step_timestamps():
    stats = load("stats_0", screenshot=False)
    later = load("stats_6", screenshot=False)
    driver = StubDriver(observations=[later])
    check = _spinner_check(spinner_wait=False, spinner_timeout_s=5.0)
    assert check.run(StepContext(after=stats, driver=driver, since_ts=stats.ts - 1)) == []
    later.ts = stats.ts + 6
    issues = check.run(StepContext(after=later, driver=driver, since_ts=later.ts - 1))
    assert [i.element.identifier for i in issues] == ["stats.loadingSpinner"]
    assert driver.observe_calls == 0


# Console noise ---------------------------------------------------------------


def _log_entries() -> list[LogEntry]:
    raw = json.loads((FIXTURES / "ios_log_noise.json").read_text())
    return [LogEntry(ts=e["ts"], level=e["level"], message=e["message"], subsystem=e["subsystem"], process=e["process"]) for e in raw]


def _console(logs) -> list:
    home = load("home", screenshot=False)
    check = FunctionalCheck(FunctionalSettings(endless_spinners=False))
    return check.run(StepContext(after=home, driver=StubDriver(logs=logs), since_ts=home.ts - 1))


def test_ios_system_log_noise_is_not_reported():
    assert _console(_log_entries()) == []


def test_app_errors_are_still_reported():
    own = LogEntry(ts=1.0, level="error", message="Sync failed: 500", subsystem="dev.swarmqa.PlantedBugs")
    plain = LogEntry(ts=1.0, level="fault", message="Unexpected nil attachment data", subsystem="")
    issues = _console(_log_entries() + [own, plain])
    assert len(issues) == 1
    assert "Sync failed" in issues[0].details and "Unexpected nil" in issues[0].details
    assert issues[0].severity == "medium"  # a fault is in the set


def test_dead_tap_seen_from_a_mid_transition_screen():
    # The explorer often observes right after a push, so `before` still holds
    # the sliding home page and a mid-slide screenshot.
    before = load("push_report_0")
    after = load("detail_report_after_share")
    action = StepDecision(kind="tap", target=ElementQuery(identifier="detail.shareButton"))
    ctx = StepContext(after=after, before=before, driver=StubDriver(), action=action, since_ts=after.ts - 1)
    issues = FunctionalCheck(FunctionalSettings(endless_spinners=False)).run(ctx)
    assert [i.element.identifier for i in issues if i.kind == "unresponsive"] == ["detail.shareButton"]


def _dead(before, after, identifier):
    action = StepDecision(kind="tap", target=ElementQuery(identifier=identifier))
    ctx = StepContext(after=after, before=before, driver=StubDriver(), action=action, since_ts=after.ts - 1)
    issues = FunctionalCheck(FunctionalSettings(endless_spinners=False)).run(ctx)
    return [i for i in issues if i.kind == "unresponsive"]


def test_tap_on_toggle_row_centre_is_not_a_dead_tap():
    # XCUITest taps the row's centre, which on iOS misses the switch itself.
    before = load("detail_report", screenshot=False)
    after = load("detail_report_after_share", screenshot=False)
    assert _dead(before, after, "detail.completeToggle") == []


def test_tap_next_to_the_keyboard_is_not_a_dead_tap():
    # With the keyboard up, the Advanced row sits right above the suggestion bar.
    screen = load("settings_keyboard", screenshot=False)
    assert _dead(screen, screen, "settings.advancedLink") == []


def test_press_highlight_on_the_tapped_button_is_not_a_response(tmp_path):
    from PIL import Image, ImageDraw

    before = load("detail_report", screenshot=False)
    after = load("detail_report_after_share", screenshot=False)
    scale = before.scale
    size = (int(before.size[0] * scale), int(before.size[1] * scale))
    first = Image.new("RGB", size, "white")
    second = first.copy()
    x, y, w, h = 20, 461.667, 95, 36.3333  # detail.shareButton, dimmed while pressed
    ImageDraw.Draw(second).rectangle((x * scale, y * scale, (x + w) * scale, (y + h) * scale), fill=(120, 160, 250))
    first.save(tmp_path / "before.png")
    second.save(tmp_path / "after.png")
    before.screenshot, after.screenshot = tmp_path / "before.png", tmp_path / "after.png"
    assert [i.element.identifier for i in _dead(before, after, "detail.shareButton")] == ["detail.shareButton"]
    # A change elsewhere on screen still counts as a response.
    ImageDraw.Draw(second).rectangle((0, 1500, size[0], 2400), fill=(0, 0, 0))
    second.save(tmp_path / "after.png")
    assert _dead(before, after, "detail.shareButton") == []


# Crash reports -----------------------------------------------------------------


def _stamp(ts: float) -> str:
    from datetime import datetime, timezone

    # The .ips captureTime format: "2026-09-30 23:02:08.9239 +0100".
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-2] + " +0000"


class CrashyDriver(StubDriver):
    def __init__(self, reports):
        super().__init__()
        self.reports = reports

    def crash_reports_since(self, ts):
        return list(self.reports)


def test_late_written_crash_report_from_an_earlier_session_is_ignored():
    # Seen live: a crash's .ips was written ~47 s after the crash, during the
    # next shard's first step, whose app was alive and answering.
    from swarmqa.driver.protocol import CrashReport

    home = load("home", screenshot=False)
    since = home.ts - 1
    old = CrashReport(ts=since + 0.5, process="PlantedBugs", path="old.ips", extra={"capture_time": _stamp(since - 47)})
    new = CrashReport(ts=since + 0.5, process="PlantedBugs", path="new.ips", extra={"capture_time": _stamp(since + 0.2)})
    check = FunctionalCheck(FunctionalSettings(endless_spinners=False, dead_taps=False))
    assert check.run(StepContext(after=home, driver=CrashyDriver([old]), since_ts=since)) == []
    issues = check.run(StepContext(after=home, driver=CrashyDriver([new]), since_ts=since))
    assert [i.kind for i in issues] == ["crash"]
