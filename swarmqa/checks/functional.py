"""Functional checks: crashes, hangs, dead taps, endless spinners, error alerts, console errors.

`FunctionalCheck` runs after every step (`per_screen = False`). Every rule is
conservative: when the evidence is ambiguous it reports nothing. See
docs/checks.md for the rules and thresholds.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from swarmqa.checks._tree import (
    FIELD_ROLES,
    INTERACTIVE_ROLES,
    MODAL_ROLES,
    PROGRESS_ROLES,
    TEXT_ROLES,
    TOGGLE_ROLES,
    describe,
    nodes,
    norm_role,
    query_for,
    screen_frame,
    stale_nodes,
    subtree_text,
    usable_frame,
    visible_signature,
    walk,
)
from swarmqa.checks.protocol import CheckIssue, StepContext
from swarmqa.driver.query import matches_query
from swarmqa.models import ElementQuery, UIElement

_ERROR_TEXT = re.compile(
    r"\b(error|errors|failed|failure|went wrong|couldn['’]t|could not|"
    r"unable to|try again later)\b",
    re.IGNORECASE,
)
_TIMEOUT_TEXT = re.compile(
    r"(uitimeouterror|timed out|timeout|time out|not responding|unresponsive|\bhung\b|\bhang(?:s|ing)?\b)",
    re.IGNORECASE,
)
# Framework and test-harness log lines that show up at error level in every
# healthy iOS app run under XCUITest. Matched against the message.
SYSTEM_LOG_NOISE = (
    r"System gesture gate timed out",
    r"animationDidStop without a matching animationDidStart",
    r"UIKeyboardLayoutStar",
    r"Automation type mismatch",
    r"fopen failed for data file",
    r"Snapshotting a view .* not in a visible window",
    r"Failed to send CA Event",
    r"\[UIKeyboard",
    r"RTIInputSystemClient",
    r"Could not find cached accumulator",
    r"CHHapticPattern",
)
_NUMBERS = re.compile(r"0x[0-9a-f]+|\d+", re.IGNORECASE)
_TITLE_LIMIT = 100


@dataclass
class FunctionalSettings:
    """Tunables for `FunctionalCheck`.

    `hang_after_s` flags a step whose observation came more than that many
    seconds after the action started (0 turns the timing rule off).
    `pixel_change_tolerance` is the fraction of changed screenshot pixels
    above which a tap with an unchanged tree still counts as having done
    something. `console_ignore` are regexes for log lines to drop, on top of
    `SYSTEM_LOG_NOISE`. With `console_app_only`, lines from system
    subsystems (`console_system_subsystems` prefixes, `com.apple.` by
    default) are dropped unless the subsystem is the app's own bundle id:
    the app process hosts UIKit, SwiftUI and XCTest, which log errors in
    healthy runs. `spinner_timeout_s` is how long an activity indicator may
    stay on screen before it counts as endless; with `spinner_wait` the check
    re-observes the screen (tree only) until the spinner goes or the time is
    up, instead of relying on the explorer to linger.
    """

    crashes: bool = True
    hangs: bool = True
    dead_taps: bool = True
    error_alerts: bool = True
    console_errors: bool = True
    hang_after_s: float = 15.0
    pixel_change_tolerance: float = 0.005
    endless_spinners: bool = True
    console_ignore: list[str] = field(default_factory=list)
    console_app_only: bool = True
    console_system_subsystems: list[str] = field(default_factory=lambda: ["com.apple."])
    max_console_lines: int = 5
    spinner_timeout_s: float = 10.0
    spinner_wait: bool = True
    spinner_poll_s: float = 1.0


class FunctionalCheck:
    name = "functional"
    per_screen = False

    def __init__(self, settings: FunctionalSettings | None = None):
        self.settings = settings or FunctionalSettings()
        self._ignore = [
            re.compile(pattern) for pattern in (*SYSTEM_LOG_NOISE, *self.settings.console_ignore)
        ]
        self.last_error: str | None = None
        # Spinner key -> first time it was seen on screen, and keys already reported.
        self._spinners: dict[str, float] = {}
        self._spinners_reported: set[str] = set()
        self.sleep = time.sleep

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        self.last_error = None
        settings = self.settings
        issues: list[CheckIssue] = []
        if settings.crashes:
            crash = self._crash(ctx)
            if crash is not None:
                # A dead app explains every other symptom of this step.
                return [crash]
        if ctx.crashed and not ctx.after.tree:
            return []  # no screen to inspect after a crash
        if settings.hangs:
            issues.extend(self._hang(ctx))
        if settings.dead_taps and not issues:
            issues.extend(self._dead_tap(ctx))
        if settings.endless_spinners:
            issues.extend(self._endless_spinner(ctx))
        if settings.error_alerts:
            issues.extend(self._error_alerts(ctx))
        if settings.console_errors:
            issues.extend(self._console(ctx))
        for issue in issues:
            issue.check = issue.check or self.name
            if issue.screenshot is None:
                issue.screenshot = ctx.after.screenshot
        return issues

    # Crash --------------------------------------------------------------

    def _crash(self, ctx: StepContext) -> CheckIssue | None:
        reports = []
        try:
            # since_ts <= 0 means "unknown"; asking would return every old report.
            if ctx.since_ts > 0:
                reports = list(ctx.driver.crash_reports_since(ctx.since_ts))
        except Exception as exc:  # driver trouble is not an app finding
            self.last_error = f"crash_reports_since failed: {exc}"
        if not ctx.crashed and ctx.after.tree:
            # The app answered after this step, so a report whose crash
            # happened before the step started is a late-written report from
            # an earlier session (ReportCrash can take seconds to write the
            # .ips, and the driver filters by file time).
            reports = [r for r in reports if not _crashed_before(r, ctx.since_ts)]
        if not ctx.crashed and not reports:
            return None
        lines: list[str] = []
        summary = ""
        for report in reports:
            summary = summary or report.summary
            lines.append(f"{report.process}: {report.summary or 'no summary'} ({report.path})")
        if ctx.error:
            lines.insert(0, f"driver: {ctx.error}")
        # Same title and target as the agent loop's own "Crash on <target>"
        # issue, so a crash keeps one fingerprint whichever side reports it.
        element, target = _action_target(ctx)
        if target:
            title = f"Crash on {target}"
        elif summary:
            title = f"App crashed: {_clip(_normalise(summary))}"
        else:
            title = "App crashed"
        extra = {"crash_report": reports[0].path} if reports else {}
        # After a crash the loop passes an empty tree and no screenshot; fall
        # back to the pre-action screenshot as evidence.
        shot = ctx.after.screenshot
        if shot is None and ctx.before is not None:
            shot = ctx.before.screenshot
        return CheckIssue(
            kind="crash",
            category="crash",
            title=title,
            severity="critical",
            details="\n".join(lines) or "The app died during the action.",
            element=element,
            check=self.name,
            screenshot=shot,
            extra=extra,
        )

    # Hang ---------------------------------------------------------------

    def _hang(self, ctx: StepContext) -> list[CheckIssue]:
        target = ctx.action.target if ctx.action is not None else None
        if ctx.error and _TIMEOUT_TEXT.search(ctx.error):
            return [
                CheckIssue(
                    kind="unresponsive",
                    category="broken",
                    title="App stopped responding",
                    severity="high",
                    details=f"The action timed out: {ctx.error}",
                    element=target,
                    check=self.name,
                )
            ]
        limit = self.settings.hang_after_s
        if limit > 0 and ctx.action is not None and ctx.since_ts > 0:
            elapsed = ctx.after.ts - ctx.since_ts
            if elapsed > limit:
                return [
                    CheckIssue(
                        kind="unresponsive",
                        category="broken",
                        title="App was slow to respond",
                        severity="medium",
                        confidence=0.6,
                        advisory=True,
                        details=(
                            f"The screen settled {elapsed:.1f}s after the action "
                            f"(limit {limit:g}s). Slow hosts can cause this."
                        ),
                        element=target,
                        check=self.name,
                    )
                ]
        return []

    # Dead tap -----------------------------------------------------------

    def _dead_tap(self, ctx: StepContext) -> list[CheckIssue]:
        action = ctx.action
        before = ctx.before
        if action is None or before is None or ctx.error or ctx.crashed:
            return []
        if action.kind not in ("tap", "tap_point"):
            return []
        element = _tapped_element(before.tree, action)
        if element is None or not element.enabled:
            return []
        role = norm_role(element.role)
        if role not in INTERACTIVE_ROLES or role in FIELD_ROLES:
            # Fields take focus without changing the tree; static text and
            # containers are not promised to do anything.
            return []
        if role in ("slider", "stepper", "incrementor"):
            return []
        if _has_interactive_descendant(element):
            # A labelled row around the real control (a SwiftUI Toggle row
            # holds the switch): a tap on the row's centre can miss the
            # control without the app being at fault.
            return []
        if _near_keyboard(before.tree, element):
            return []  # the tap may have landed on the keyboard or its suggestion bar
        if visible_signature(before.tree, before.size) != visible_signature(ctx.after.tree, ctx.after.size):
            return []  # something moved, appeared, or changed value
        if _new_modal(before.tree, ctx.after.tree):
            return []
        # A screenshot taken while a page was still sliding in cannot be
        # compared; the tree (which already has the final frames) decides.
        settled = not _in_transition(before) and not _in_transition(ctx.after)
        if settled and _pixels_changed(
            before.screenshot,
            ctx.after.screenshot,
            self.settings.pixel_change_tolerance,
            ignore=usable_frame(element),
            scale=before.scale or 1.0,
        ):
            return []
        what = "toggle" if role in TOGGLE_ROLES else "control"
        return [
            CheckIssue(
                kind="unresponsive",
                category="broken",
                title=f"Tapping {describe(element)} did nothing",
                severity="medium",
                confidence=0.8,
                details=(
                    f"The {what} is enabled, but after the tap the accessibility tree "
                    "(elements, values, frames) and the screenshot were unchanged and "
                    "no alert appeared."
                ),
                element=query_for(element),
                bbox=usable_frame(element),
                check=self.name,
            )
        ]

    # Endless spinner ----------------------------------------------------

    def _endless_spinner(self, ctx: StepContext) -> list[CheckIssue]:
        """An activity indicator still spinning `spinner_timeout_s` after it first appeared.

        The first sighting starts a clock. With `spinner_wait`, the check then
        polls `driver.observe(screenshot=False)` until the spinner goes or the
        time runs out, so the verdict does not depend on the explorer staying
        on the screen. Each spinner is reported once per session.
        """
        settings = self.settings
        spinners = _spinners_on_screen(ctx.after)
        now = ctx.after.ts or time.time()
        for key in list(self._spinners):
            if key not in spinners:
                del self._spinners[key]  # it finished
        issues: list[CheckIssue] = []
        for key, element in spinners.items():
            if key in self._spinners_reported:
                continue
            first = self._spinners.setdefault(key, now)
            elapsed = now - first
            if elapsed < settings.spinner_timeout_s and settings.spinner_wait:
                elapsed, element = self._wait_for_spinner(ctx, key, first, element)
                if element is None:
                    self._spinners.pop(key, None)
                    continue
            if elapsed < settings.spinner_timeout_s:
                continue
            self._spinners_reported.add(key)
            name = element.label or element.identifier or "activity indicator"
            issues.append(
                CheckIssue(
                    kind="timeout",
                    category="broken",
                    title=f"Loading never finished: {_clip(name)}",
                    severity="medium",
                    confidence=0.8,
                    details=(
                        f"An activity indicator was still on screen {elapsed:.0f}s after it "
                        f"appeared (limit {settings.spinner_timeout_s:g}s), with no content, "
                        "error or retry in its place."
                    ),
                    element=query_for(element),
                    bbox=usable_frame(element),
                    check=self.name,
                )
            )
        return issues

    def _wait_for_spinner(
        self, ctx: StepContext, key: str, first: float, element: UIElement
    ) -> tuple[float, UIElement | None]:
        """Poll until `key` leaves the screen or the timeout passes. Returns (elapsed, element or None)."""
        settings = self.settings
        observe = getattr(ctx.driver, "observe", None)
        if observe is None:
            return 0.0, element
        start = time.monotonic()
        waited_from = (ctx.after.ts or time.time()) - first
        while True:
            elapsed = waited_from + (time.monotonic() - start)
            if elapsed >= settings.spinner_timeout_s:
                return elapsed, element
            self.sleep(max(0.05, settings.spinner_poll_s))
            try:
                obs = observe(None, screenshot=False)
            except Exception as exc:  # driver trouble is not an app finding
                self.last_error = f"observe while waiting for a spinner failed: {exc}"
                return 0.0, None
            current = _spinners_on_screen(obs).get(key)
            if current is None:
                return waited_from + (time.monotonic() - start), None
            element = current

    # Error alert --------------------------------------------------------

    def _error_alerts(self, ctx: StepContext) -> list[CheckIssue]:
        seen_before = set()
        if ctx.before is not None:
            seen_before = {subtree_text(e) for e in _modals(ctx.before.tree)}
        issues: list[CheckIssue] = []
        for modal in _modals(ctx.after.tree):
            text = subtree_text(modal)
            if text in seen_before or not _ERROR_TEXT.search(text):
                continue
            headline = modal.label or _first_text(modal) or text
            issues.append(
                CheckIssue(
                    kind="error_state",
                    category="broken",
                    title=f"Error shown: {_clip(headline)}",
                    severity="high",
                    details=f"A {norm_role(modal.role)} appeared with error text: {text}",
                    element=query_for(modal),
                    bbox=usable_frame(modal),
                    check=self.name,
                )
            )
        return issues

    # Console errors -----------------------------------------------------

    def _system_subsystem(self, subsystem: str, app_subsystem: str) -> bool:
        if not self.settings.console_app_only or not subsystem:
            return False
        if app_subsystem and (subsystem == app_subsystem or subsystem.startswith(app_subsystem + ".")):
            return False
        return any(subsystem.startswith(prefix) for prefix in self.settings.console_system_subsystems)

    def _console(self, ctx: StepContext) -> list[CheckIssue]:
        if ctx.since_ts <= 0:
            return []
        try:
            entries = list(ctx.driver.logs_since(ctx.since_ts))
        except Exception as exc:
            self.last_error = f"logs_since failed: {exc}"
            return []
        app_subsystem = str(getattr(ctx.driver, "bundle_id", "") or "")
        errors = [
            entry
            for entry in entries
            if entry.level in ("error", "fault")
            and not self._system_subsystem(entry.subsystem, app_subsystem)
            and not any(pattern.search(entry.message) for pattern in self._ignore)
        ]
        if not errors:
            return []
        fault = any(entry.level == "fault" for entry in errors)
        first = errors[0]
        shown = errors[: self.settings.max_console_lines]
        lines = [f"[{e.level}] {e.subsystem + ': ' if e.subsystem else ''}{e.message}" for e in shown]
        if len(errors) > len(shown):
            lines.append(f"... and {len(errors) - len(shown)} more")
        return [
            CheckIssue(
                kind="error_state",
                category="broken",
                title=f"Console {first.level}: {_clip(_normalise(first.message))}",
                severity="medium" if fault else "low",
                confidence=0.5,
                advisory=True,
                details="\n".join(lines),
                check=self.name,
                extra={"log_lines": str(len(errors))},
            )
        ]


def _spinners_on_screen(obs) -> dict[str, UIElement]:
    """Visible activity indicators on the page on screen, keyed for tracking across steps.

    A progress element nested in another (SwiftUI's `ProgressView("…")`
    wraps the system spinner) counts once, as the outermost one.
    """
    flat = nodes(obs.tree)
    stale = stale_nodes(flat, screen_frame(obs.tree, obs.size))
    found: dict[str, UIElement] = {}
    for node in flat:
        if node.index in stale or node.role not in PROGRESS_ROLES:
            continue
        if any(flat[index].role in PROGRESS_ROLES for index in node.ancestors):
            continue
        element = node.element
        if usable_frame(element) is None:
            continue
        if node.role == "activityindicator" and (element.value or "").strip() in ("0", "false"):
            continue  # a stopped (hidden) spinner
        key = element.identifier or element.label or "spinner"
        found.setdefault(key, element)
    return found


def _crashed_before(report, ts: float, slack: float = 2.0) -> bool:
    """The report's own crash time (`extra["capture_time"]`) is before `ts`. False when unknown."""
    text = (getattr(report, "extra", {}) or {}).get("capture_time", "")
    if not text:
        return False
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            when = datetime.strptime(text.strip(), fmt).timestamp()
        except ValueError:
            continue
        return when < ts - slack
    return False


def _in_transition(obs) -> bool:
    """The observation caught a navigation transition (a page off the origin)."""
    flat = nodes(obs.tree)
    return bool(stale_nodes(flat, screen_frame(obs.tree, obs.size)))


def _action_target(ctx: StepContext) -> tuple[ElementQuery | None, str]:
    """The action's target query and its dedup name (identifier, else label, else role)."""
    action = ctx.action
    if action is None:
        return None, ""
    query = action.target
    if query is not None:
        name = query.identifier or query.label or query.role or ""
        return (query if name else None), name
    if action.point is not None:
        return None, f"({action.point[0]:g}, {action.point[1]:g})"
    return None, ""


def _tapped_element(tree: list[UIElement], action) -> UIElement | None:
    if action.kind == "tap" and action.target is not None:
        query = action.target
        if not any((query.role, query.label, query.identifier, query.value)):
            return None
        for element in walk(tree):
            if matches_query(element, query):
                return element
        return None
    if action.kind == "tap_point" and action.point is not None:
        x, y = action.point
        best: UIElement | None = None
        best_area = float("inf")
        smallest_any: UIElement | None = None
        smallest_any_area = float("inf")
        for element in walk(tree):
            frame = usable_frame(element)
            if frame is None:
                continue
            left, top, width, height = frame
            if not (left <= x <= left + width and top <= y <= top + height):
                continue
            size = width * height
            if size < smallest_any_area:
                smallest_any, smallest_any_area = element, size
            if norm_role(element.role) in INTERACTIVE_ROLES and size < best_area:
                best, best_area = element, size
        if smallest_any is not None and norm_role(smallest_any.role) in TEXT_ROLES and best is None:
            return None
        return best
    return None


def _modals(tree: list[UIElement]) -> list[UIElement]:
    return [element for element in walk(tree) if norm_role(element.role) in MODAL_ROLES]


def _new_modal(before: list[UIElement], after: list[UIElement]) -> bool:
    return len(_modals(after)) > len(_modals(before))


def _first_text(element: UIElement) -> str:
    for item in walk(element.children):
        if norm_role(item.role) in TEXT_ROLES and item.label:
            return item.label
    return ""


def _pixels_changed(
    before: Path | None,
    after: Path | None,
    tolerance: float,
    *,
    ignore: tuple[float, float, float, float] | None = None,
    scale: float = 1.0,
    padding: float = 4.0,
) -> bool:
    """True when both screenshots exist and differ in more than `tolerance` of pixels.

    `ignore` is a frame in points (the tapped control) blanked in both images
    first: its own press highlight fading out is not a response to the tap.
    """
    if before is None or after is None:
        return False
    from PIL import ImageDraw

    from swarmqa.checks.baseline import diff_images, load_rgba

    try:
        left = load_rgba(Path(before))
        right = load_rgba(Path(after))
    except Exception:
        return True  # unreadable evidence: do not accuse the app
    try:
        if ignore is not None and left.size == right.size:
            x, y, width, height = ignore
            box = (
                int((x - padding) * scale),
                int((y - padding) * scale),
                int((x + width + padding) * scale),
                int((y + height + padding) * scale),
            )
            for image in (left, right):
                ImageDraw.Draw(image).rectangle(box, fill=(0, 0, 0, 255))
        result = diff_images(left, right)
    finally:
        left.close()
        right.close()
    if result is None:
        return True  # sizes differ: the screen changed
    return result.score > tolerance


def _has_interactive_descendant(element: UIElement) -> bool:
    for item in walk(element.children):
        if norm_role(item.role) in INTERACTIVE_ROLES:
            return True
    return False


def _near_keyboard(tree: list[UIElement], element: UIElement, margin: float = 60.0) -> bool:
    """A keyboard is up and the element reaches into it or the bar just above it."""
    frame = usable_frame(element)
    if frame is None:
        return False
    for item in walk(tree):
        if norm_role(item.role) != "keyboard":
            continue
        keyboard = usable_frame(item)
        if keyboard is not None and frame[1] + frame[3] > keyboard[1] - margin:
            return True
    return False


def _normalise(text: str) -> str:
    """One line with numbers and addresses replaced, so titles dedup across runs."""
    return _NUMBERS.sub("N", " ".join(str(text).split()))


def _clip(text: str) -> str:
    one_line = " ".join(str(text).split())
    if len(one_line) <= _TITLE_LIMIT:
        return one_line
    return one_line[: _TITLE_LIMIT - 3].rstrip() + "..."
