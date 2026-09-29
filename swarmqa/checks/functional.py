"""Functional checks: crashes, hangs, dead taps, error alerts, console errors.

`FunctionalCheck` runs after every step (`per_screen = False`). Every rule is
conservative: when the evidence is ambiguous it reports nothing. See
docs/checks.md for the rules and thresholds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from swarmqa.checks._tree import (
    FIELD_ROLES,
    INTERACTIVE_ROLES,
    MODAL_ROLES,
    TEXT_ROLES,
    TOGGLE_ROLES,
    describe,
    norm_role,
    query_for,
    structure_signature,
    subtree_text,
    usable_frame,
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
_NUMBERS = re.compile(r"0x[0-9a-f]+|\d+", re.IGNORECASE)
_TITLE_LIMIT = 100


@dataclass
class FunctionalSettings:
    """Tunables for `FunctionalCheck`.

    `hang_after_s` flags a step whose observation came more than that many
    seconds after the action started (0 turns the timing rule off).
    `pixel_change_tolerance` is the fraction of changed screenshot pixels
    above which a tap with an unchanged tree still counts as having done
    something. `console_ignore` are regexes for log lines to drop.
    """

    crashes: bool = True
    hangs: bool = True
    dead_taps: bool = True
    error_alerts: bool = True
    console_errors: bool = True
    hang_after_s: float = 15.0
    pixel_change_tolerance: float = 0.005
    console_ignore: list[str] = field(default_factory=list)
    max_console_lines: int = 5


class FunctionalCheck:
    name = "functional"
    per_screen = False

    def __init__(self, settings: FunctionalSettings | None = None):
        self.settings = settings or FunctionalSettings()
        self._ignore = [re.compile(pattern) for pattern in self.settings.console_ignore]
        self.last_error: str | None = None

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
        if structure_signature(before.tree) != structure_signature(ctx.after.tree):
            return []  # something moved, appeared, or changed value
        if _new_modal(before.tree, ctx.after.tree):
            return []
        if _pixels_changed(before.screenshot, ctx.after.screenshot, self.settings.pixel_change_tolerance):
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

    def _console(self, ctx: StepContext) -> list[CheckIssue]:
        if ctx.since_ts <= 0:
            return []
        try:
            entries = list(ctx.driver.logs_since(ctx.since_ts))
        except Exception as exc:
            self.last_error = f"logs_since failed: {exc}"
            return []
        errors = [
            entry
            for entry in entries
            if entry.level in ("error", "fault")
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


def _pixels_changed(before: Path | None, after: Path | None, tolerance: float) -> bool:
    """True when both screenshots exist and differ in more than `tolerance` of pixels."""
    if before is None or after is None:
        return False
    from swarmqa.checks.baseline import diff_files

    try:
        result = diff_files(Path(before), Path(after))
    except Exception:
        return True  # unreadable evidence: do not accuse the app
    if result is None:
        return True  # sizes differ: the screen changed
    return result > tolerance


def _normalise(text: str) -> str:
    """One line with numbers and addresses replaced, so titles dedup across runs."""
    return _NUMBERS.sub("N", " ".join(str(text).split()))


def _clip(text: str) -> str:
    one_line = " ".join(str(text).split())
    if len(one_line) <= _TITLE_LIMIT:
        return one_line
    return one_line[: _TITLE_LIMIT - 3].rstrip() + "..."
