"""WP-B3 — functional checks: crash, hang, dead tap, error alert, console errors."""

from __future__ import annotations

import pytest
from PIL import Image

from swarmqa.checks import Check, FunctionalCheck, FunctionalSettings
from swarmqa.checks.protocol import StepContext
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import ElementQuery, UIElement
from swarmqa.testing import make_app


@pytest.fixture
def driver(tmp_path):
    fake = FakeDriver(make_app(tmp_path), tmp_path / "work")
    fake.launch()
    return fake


def _screen(*children: UIElement) -> list[UIElement]:
    return [UIElement(role="window", label="Main", frame=(0, 0, 390, 844), children=list(children))]


def _button(label="Save", **kwargs) -> UIElement:
    return UIElement(role="button", label=label, frame=(20, 100, 120, 44), **kwargs)


def _obs(tree, ts=100.0, shot=None) -> ScreenObservation:
    return ScreenObservation(tree=tree, ts=ts, screenshot=shot, size=(390, 844))


def _tap(label="Save", role=None) -> StepDecision:
    return StepDecision(kind="tap", target=ElementQuery(role=role, label=label))


def _ctx(driver, before, after, action=None, **kwargs) -> StepContext:
    return StepContext(
        after=_obs(after, ts=kwargs.pop("after_ts", 100.5)),
        before=None if before is None else _obs(before, ts=99.0),
        driver=driver,
        action=action,
        since_ts=kwargs.pop("since_ts", 100.0),
        **kwargs,
    )


def _kinds(issues):
    return [(issue.kind, issue.category) for issue in issues]


def test_functional_check_implements_contract():
    check = FunctionalCheck()
    assert isinstance(check, Check)
    assert check.per_screen is False


# Crash ------------------------------------------------------------------


def test_crash_from_flag_with_loop_convention(driver, tmp_path):
    # The agent loop passes an empty tree and no screenshot after a crash.
    shot = tmp_path / "before.png"
    Image.new("RGB", (4, 4), "white").save(shot)
    ctx = StepContext(
        after=ScreenObservation(tree=[], ts=101.0),
        before=ScreenObservation(tree=_screen(_button()), ts=99.0, screenshot=shot),
        driver=driver,
        action=_tap(),
        since_ts=100.0,
        crashed=True,
        error="AppCrashedError: app exited",
    )
    issues = FunctionalCheck().run(ctx)
    assert _kinds(issues) == [("crash", "crash")]
    issue = issues[0]
    assert issue.severity == "critical" and issue.check == "functional"
    assert issue.title == "Crash on Save"
    assert issue.target() == "Save"
    assert issue.screenshot == shot


def test_crashed_empty_screen_runs_nothing_else(driver):
    ctx = StepContext(
        after=ScreenObservation(tree=[], ts=101.0),
        before=ScreenObservation(tree=_screen(_button()), ts=99.0),
        driver=driver,
        action=_tap(),
        since_ts=100.0,
        crashed=True,
    )
    assert FunctionalCheck(FunctionalSettings(crashes=False)).run(ctx) == []
    from swarmqa.checks import LayoutCheck

    assert LayoutCheck().run(ctx) == []


def test_crash_from_report_includes_summary_and_path(driver):
    report = driver.add_crash("EXC_BAD_ACCESS in ListView 0x1234", ts=100.2)
    tree = _screen(_button())
    issues = FunctionalCheck().run(_ctx(driver, None, tree, None))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "crash"
    assert "EXC_BAD_ACCESS" in issue.title and "0x" not in issue.title
    assert report.path in issue.details
    assert issue.extra["crash_report"] == report.path
    tapped = FunctionalCheck().run(_ctx(driver, tree, tree, _tap()))
    assert tapped[0].title == "Crash on Save" and "EXC_BAD_ACCESS" in tapped[0].details
    point = FunctionalCheck().run(_ctx(driver, tree, tree, StepDecision(kind="tap_point", point=(50, 120.5))))
    assert point[0].title == "Crash on (50, 120.5)"


def test_old_crash_report_is_ignored(driver):
    driver.add_crash("old", ts=50.0)
    tree = _screen(_button())
    changed = _screen(_button(), UIElement(role="text", label="Saved"))
    assert FunctionalCheck().run(_ctx(driver, tree, changed, _tap())) == []


# Hang -------------------------------------------------------------------


def test_hang_from_timeout_error(driver):
    tree = _screen(_button())
    issues = FunctionalCheck().run(
        _ctx(driver, tree, tree, _tap(), error="UITimeoutError: tap did not return in 30s")
    )
    assert _kinds(issues) == [("unresponsive", "broken")]
    assert issues[0].severity == "high" and not issues[0].advisory


def test_hang_from_slow_observation_is_advisory(driver):
    tree = _screen(_button())
    changed = _screen(_button(), UIElement(role="text", label="Done"))
    issues = FunctionalCheck(FunctionalSettings(hang_after_s=5)).run(
        _ctx(driver, tree, changed, _tap(), since_ts=100.0, after_ts=120.0)
    )
    assert _kinds(issues) == [("unresponsive", "broken")]
    assert issues[0].advisory and issues[0].confidence < 1


def test_word_change_is_not_a_hang(driver):
    tree = _screen(_button())
    changed = _screen(_button(), UIElement(role="text", label="Changed"))
    assert FunctionalCheck().run(_ctx(driver, tree, changed, _tap(), error="value change rejected")) == []


# Dead tap ---------------------------------------------------------------


def test_dead_tap_on_enabled_button(driver):
    tree = _screen(_button())
    issues = FunctionalCheck().run(_ctx(driver, tree, _screen(_button()), _tap()))
    assert _kinds(issues) == [("unresponsive", "broken")]
    assert issues[0].element.label == "Save"
    assert issues[0].bbox == (20.0, 100.0, 120.0, 44.0)


def test_dead_tap_point(driver):
    tree = _screen(_button())
    action = StepDecision(kind="tap_point", point=(50, 120))
    issues = FunctionalCheck().run(_ctx(driver, tree, _screen(_button()), action))
    assert _kinds(issues) == [("unresponsive", "broken")]


def test_tap_that_changes_tree_is_fine(driver):
    before = _screen(_button())
    after = _screen(UIElement(role="text", label="Next screen", frame=(0, 0, 100, 20)))
    assert FunctionalCheck().run(_ctx(driver, before, after, _tap())) == []


def test_toggle_whose_value_changed_is_fine(driver):
    def toggle(value):
        return UIElement(role="checkbox", label="Wi-Fi", value=value, frame=(300, 100, 51, 31))

    issues = FunctionalCheck().run(_ctx(driver, _screen(toggle("0")), _screen(toggle("1")), _tap("Wi-Fi")))
    assert issues == []


def test_toggle_whose_value_did_not_change_is_dead(driver):
    toggle = UIElement(role="checkbox", label="Wi-Fi", value="0", frame=(300, 100, 51, 31))
    issues = FunctionalCheck().run(_ctx(driver, _screen(toggle), _screen(toggle), _tap("Wi-Fi")))
    assert _kinds(issues) == [("unresponsive", "broken")]


def test_text_field_focus_is_not_a_dead_tap(driver):
    field = UIElement(role="textfield", label="Email", frame=(20, 100, 300, 34))
    assert FunctionalCheck().run(_ctx(driver, _screen(field), _screen(field), _tap("Email"))) == []


def test_static_text_tap_is_not_a_dead_tap(driver):
    text = UIElement(role="text", label="Welcome", frame=(20, 100, 300, 20))
    assert FunctionalCheck().run(_ctx(driver, _screen(text), _screen(text), _tap("Welcome"))) == []
    point = StepDecision(kind="tap_point", point=(40, 110))
    assert FunctionalCheck().run(_ctx(driver, _screen(text), _screen(text), point)) == []


def test_disabled_button_is_not_a_dead_tap(driver):
    tree = _screen(_button(enabled=False))
    assert FunctionalCheck().run(_ctx(driver, tree, tree, _tap())) == []


def test_alert_after_tap_is_not_dead(driver):
    before = _screen(_button())
    after = _screen(_button()) + [UIElement(role="alert", label="Saved", frame=(50, 300, 290, 150))]
    assert FunctionalCheck().run(_ctx(driver, before, after, _tap())) == []


def test_screenshot_change_is_not_dead(driver, tmp_path):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    Image.new("RGB", (40, 40), "white").save(first)
    Image.new("RGB", (40, 40), "black").save(second)
    tree = _screen(_button())
    ctx = StepContext(
        after=ScreenObservation(tree=_screen(_button()), ts=100.5, screenshot=second),
        before=ScreenObservation(tree=tree, ts=99.0, screenshot=first),
        driver=driver,
        action=_tap(),
        since_ts=100.0,
    )
    assert FunctionalCheck().run(ctx) == []


def test_first_screen_has_no_dead_tap(driver):
    tree = _screen(_button())
    assert FunctionalCheck().run(_ctx(driver, None, tree, None)) == []


# Error alert ------------------------------------------------------------


def test_error_alert(driver):
    alert = UIElement(
        role="alert",
        label="Something went wrong",
        frame=(50, 300, 290, 150),
        children=[
            UIElement(role="text", label="We couldn't save your note."),
            UIElement(role="button", label="OK"),
        ],
    )
    before = _screen(_button())
    after = _screen(_button()) + [alert]
    issues = FunctionalCheck().run(_ctx(driver, before, after, _tap()))
    assert _kinds(issues) == [("error_state", "broken")]
    assert "Something went wrong" in issues[0].title
    assert "couldn't save" in issues[0].details


def test_benign_alert_is_not_an_error(driver):
    alert = UIElement(role="alert", label="Delete note?", children=[UIElement(role="button", label="Delete")])
    before = _screen(_button())
    assert FunctionalCheck().run(_ctx(driver, before, _screen(_button()) + [alert], _tap())) == []


def test_error_alert_already_shown_is_not_repeated(driver):
    alert = UIElement(role="sheet", label="Upload failed")
    tree = _screen(_button()) + [alert]
    changed = _screen(_button(), UIElement(role="text", label="x")) + [alert]
    assert FunctionalCheck().run(_ctx(driver, tree, changed, _tap())) == []


# Console ----------------------------------------------------------------


def test_console_errors_grouped_and_advisory(driver):
    driver.add_log("network ok", "info", ts=100.1)
    driver.add_log("Request 42 failed with 500", "error", ts=100.2)
    driver.add_log("CoreData fault 7", "fault", ts=100.3)
    driver.add_log("earlier", "error", ts=10.0)
    tree = _screen(_button())
    changed = _screen(_button(), UIElement(role="text", label="Saved"))
    issues = FunctionalCheck().run(_ctx(driver, tree, changed, _tap()))
    assert _kinds(issues) == [("error_state", "broken")]
    issue = issues[0]
    assert issue.advisory and issue.severity == "medium"
    assert issue.title == "Console error: Request N failed with N"
    assert "CoreData fault 7" in issue.details and "earlier" not in issue.details
    assert issue.extra["log_lines"] == "2"


def test_console_ignore_patterns(driver):
    driver.add_log("nw_connection noise", "error", ts=100.2)
    tree = _screen(_button())
    changed = _screen(_button(), UIElement(role="text", label="Saved"))
    check = FunctionalCheck(FunctionalSettings(console_ignore=[r"^nw_"]))
    assert check.run(_ctx(driver, tree, changed, _tap())) == []


def test_driver_log_failure_fails_open(driver, monkeypatch):
    def boom(ts):
        raise RuntimeError("log stream closed")

    monkeypatch.setattr(driver, "logs_since", boom)
    tree = _screen(_button())
    changed = _screen(_button(), UIElement(role="text", label="Saved"))
    check = FunctionalCheck()
    assert check.run(_ctx(driver, tree, changed, _tap())) == []
    assert "log stream closed" in check.last_error


def test_issue_becomes_finding(driver, tmp_path):
    tree = _screen(_button())
    issue = FunctionalCheck().run(_ctx(driver, tree, tree, _tap()))[0]
    finding = issue.to_finding(finding_id="f-1", worker_id="w1", shard_id="s1", backend="local", steps=["tap Save"])
    assert finding.kind == "unresponsive"
    assert finding.category == "broken"
    assert "check: functional" in finding.details


def test_unknown_since_ts_does_not_surface_old_crashes_or_logs(driver):
    driver.add_crash("old crash", ts=10.0)
    driver.add_log("old error", level="error", ts=10.0)
    tree = _screen(_button())
    issues = FunctionalCheck().run(_ctx(driver, None, tree, since_ts=0.0))
    assert issues == []
