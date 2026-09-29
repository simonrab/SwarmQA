"""Explorer v2: an observe-decide-act loop with a fresh observation every step.

Every step reads the screen again with `observe()`, fingerprints it into the
`ScreenGraph`, runs the checks on a `StepContext`, decides the next action,
and does it. Goal mode follows `shard.goal` (heuristic first, then the
ModelProvider); crawl mode visits untried controls breadth-first with no
model. Check issues become findings only through `CheckIssue.to_finding`,
each with a replay flow that `explorer/scripted.py` can run.

See docs/explorer-v2.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from swarmqa.checks.protocol import Check, CheckIssue, StepContext
from swarmqa.decision.heuristic import goal_satisfied, matches_name, parse_goal, shares
from swarmqa.driver.protocol import ScreenObservation, UnsupportedAction
from swarmqa.driver.query import find_element, matches_query, walk
from swarmqa.driver.v1_adapter import as_v2
from swarmqa.errors import AppCrashedError, AppMissingError, ElementNotFoundError, UITimeoutError
from swarmqa.explorer.screen_graph import (
    TAP_ROLES,
    Control,
    ScreenGraph,
    actionable_controls,
    control_key,
    fingerprint,
    normalize_role,
)
from swarmqa.llm.protocol import HistoryStep, ModelError, ModelProvider, StepDecision
from swarmqa.models import CampaignConfig, ElementQuery, Finding, Shard, StepResult, UIElement, WorkerResult
from swarmqa.reporter.findings import fingerprint_for, write_finding, write_replay
from swarmqa.video_policy import keep_video, should_start_video

LoopMode = Literal["goal", "crawl"]
_DEFAULT_SIZE = (390.0, 844.0)
_BACK_NAMES = frozenset({"back", "done", "close", "cancel"})


@dataclass
class AgentLoopSettings:
    """Knobs for one agent-loop session.

    `mode="goal"` follows `shard.goal` and drops to crawl when the goal is
    empty. `max_steps` counts driver actions (taps, types, swipes, keys,
    backs, relaunches), not observations. `stall_limit` ends goal mode after
    that many steps in a row that failed or left the screen unchanged.
    `per_screen_checks` turns off expensive (model) checks entirely.
    With `screenshot_every_step` off, only a screen's first visit is
    captured. `heuristic_first` lets a confident heuristic pick skip the
    model. `report_unchecked_crashes` adds a crash finding when no check
    reported one for a crashed step.
    """

    mode: LoopMode = "goal"
    max_steps: int = 40
    max_wall_time_s: float = 300.0
    max_model_calls: int = 20
    model_timeout_s: float = 30.0
    stall_limit: int = 5
    per_screen_checks: bool = True
    screenshot_every_step: bool = True
    heuristic_first: bool = True
    history_limit: int = 20
    sample_text: str = "test"
    max_crash_relaunches: int = 3
    report_unchecked_crashes: bool = True
    platform: str | None = None

    @classmethod
    def from_config(cls, config: CampaignConfig, **overrides: Any) -> "AgentLoopSettings":
        """Defaults from `config.explorer` and `config.app.platform`, then `overrides`."""
        explorer = config.explorer
        values: dict[str, Any] = {
            "max_steps": explorer.max_steps,
            "max_wall_time_s": explorer.max_time_s,
            "max_model_calls": explorer.decision.max_model_calls,
            "model_timeout_s": explorer.decision.model_timeout_s,
            "platform": config.app.platform,
        }
        values.update(overrides)
        return cls(**values)


@dataclass
class _Plan:
    decision: StepDecision | None = None
    source: str = ""
    control: str | None = None
    navigation: bool = False
    relaunch_for: str | None = None
    stop: str | None = None


@dataclass
class _Outcome:
    replay: list[dict] = field(default_factory=list)
    note: str = ""


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_dict(query: ElementQuery) -> dict[str, str]:
    payload: dict[str, str] = {}
    if query.role:
        payload["role"] = query.role
    if query.label:
        payload["label"] = query.label
    if query.identifier:
        payload["identifier"] = query.identifier
    if query.value is not None:
        payload["value"] = query.value
    return payload


def _query_text(query: ElementQuery | None) -> str:
    if query is None:
        return ""
    return query.label or query.identifier or query.value or query.role or ""


def describe(decision: StepDecision) -> str:
    """Human-readable step text for finding repro steps."""
    kind = decision.kind
    target = _query_text(decision.target)
    if kind == "tap":
        return f'tap "{target}"'
    if kind == "type":
        return f'type "{decision.text or ""}" into "{target}"'
    if kind == "tap_point" and decision.point:
        return f"tap at ({decision.point[0]:g}, {decision.point[1]:g})"
    if kind == "swipe" and decision.point and decision.end:
        (x0, y0), (x1, y1) = decision.point, decision.end
        return f"swipe ({x0:g}, {y0:g}) -> ({x1:g}, {y1:g})"
    if kind == "key":
        return "key " + "+".join(decision.keys)
    return kind


def decision_to_json(decision: StepDecision) -> dict[str, Any]:
    """Plain JSON for a graph edge. Rationale and usage are left out."""
    payload: dict[str, Any] = {"kind": decision.kind}
    if decision.target is not None:
        payload["target"] = _query_dict(decision.target)
    if decision.point is not None:
        payload["point"] = [float(decision.point[0]), float(decision.point[1])]
    if decision.end is not None:
        payload["end"] = [float(decision.end[0]), float(decision.end[1])]
    if decision.text is not None:
        payload["text"] = decision.text
    if decision.keys:
        payload["keys"] = list(decision.keys)
    return payload


def decision_from_json(data: dict[str, Any]) -> StepDecision:
    target = data.get("target")
    point = data.get("point")
    end = data.get("end")
    return StepDecision(
        kind=data["kind"],
        target=ElementQuery(**target) if target else None,
        point=(point[0], point[1]) if point else None,
        end=(end[0], end[1]) if end else None,
        text=data.get("text"),
        keys=list(data.get("keys", [])),
    )


def _control_decision(control: Control, sample_text: str) -> StepDecision:
    if control.kind == "type":
        return StepDecision(kind="type", target=control.query(), text=sample_text, rationale="crawl")
    return StepDecision(kind="tap", target=control.query(), rationale="crawl")


def exact_element(tree: list[UIElement], query: ElementQuery) -> UIElement | None:
    """The element `query` means, preferring an exact (case-insensitive) label match.

    Driver queries match labels by substring, so "Save" also matches an
    earlier "Save As"; this picks "Save".
    """
    candidates = [element for element in walk(tree) if matches_query(element, query)]
    if not candidates:
        return None
    if query.label:
        wanted = query.label.strip().lower()
        for element in candidates:
            if (element.label or "").strip().lower() == wanted:
                return element
    return candidates[0]


def precise_target(
    tree: list[UIElement], query: ElementQuery
) -> tuple[ElementQuery, tuple[float, float] | None]:
    """A query (or, failing that, a point) that reaches exactly the element `query` means.

    Returns `(query, None)` when the driver's first match already is that
    element, a query narrowed by identifier when that is unique, else
    `(query, centre)` when the element has a frame. Best effort otherwise.
    """
    element = exact_element(tree, query)
    if element is None:
        return query, None
    try:
        if find_element(tree, query) is element:
            return query, None
    except ElementNotFoundError:
        return query, None
    if element.identifier:
        narrowed = ElementQuery(role=element.role, label=query.label, identifier=element.identifier)
        try:
            if find_element(tree, narrowed) is element:
                return narrowed, None
        except ElementNotFoundError:
            pass
    if element.frame is not None:
        x, y, width, height = element.frame
        return query, (x + width / 2, y + height / 2)
    return query, None


def _campaign_context(work_dir: Path) -> tuple[Path, bool]:
    if work_dir.parent.name == "workers":
        return work_dir.parent.parent, True
    return work_dir, False


class _AgentLoop:
    def __init__(
        self,
        shard: Shard,
        driver,
        config: CampaignConfig,
        *,
        provider: ModelProvider | None,
        checks: Iterable[Check],
        settings: AgentLoopSettings,
        work_dir: Path,
        worker_id: str,
        backend: str,
        clock: Callable[[], float],
    ):
        self.shard = shard
        self.driver = as_v2(driver)
        self.config = config
        self.provider = provider
        self.checks = list(checks)
        self.settings = settings
        self.work_dir = Path(work_dir)
        self.worker_id = worker_id
        self.backend = backend
        self.clock = clock
        self.campaign, self.relative = _campaign_context(self.work_dir)
        self.goal = (shard.goal or "").strip()
        self.mode: LoopMode = "crawl" if settings.mode == "crawl" or not self.goal else "goal"
        self.tokens, self.expected = parse_goal(self.goal)
        self.platform = (settings.platform or config.app.platform or "macos").lower()
        self.graph = ScreenGraph()
        self.started = clock()
        self.steps_used = 0
        self.model_calls = 0
        self.cost = 0.0
        self.stall = 0
        self.fail_streak = 0
        self.stale = False
        self.observe_timeouts = 0
        self.pending_edge: tuple[str, dict[str, Any]] | None = None
        # Replay steps an action set out to perform, kept so a crash or
        # timeout mid-action still leaves the step in the replay.
        self.planned_replay: list[dict] = []
        self.crashes = 0
        self.observations = 0
        self.narrative: list[str] = []
        self.replay: list[dict] = []
        self.history: list[HistoryStep] = []
        self.findings: list[Finding] = []
        self.finding_replays: list[list[dict]] = []
        self.seen: set[str] = set()
        self.results: list[StepResult] = []
        self.environment: dict[str, str] = {}
        self.current: ScreenObservation | None = None
        self.current_id = ""
        self.unreachable: set[str] = set()
        self.back_tried: set[str] = set()
        self.relaunches_for: dict[str, int] = {}
        self.video_started = False
        self.fatal: str | None = None
        self.stop_reason = ""

    # Session ----------------------------------------------------------------

    def run(self) -> None:
        if not self._launch():
            return
        self._maybe_start_video()
        try:
            first = self._observe_step()
        except AppCrashedError as exc:
            self._crash_on_launch(str(exc))
            return
        except UITimeoutError as exc:
            if not self._observe_timed_out(str(exc)):
                return
        else:
            self._visit(first, before=None, action=None, since=first.ts)
        while True:
            reason = self._budget_exhausted()
            if reason:
                self.stop_reason = reason
                return
            if self.stale or self.current is None:
                if not self._refresh():
                    return
                continue
            plan = self._decide()
            if plan.stop:
                self.stop_reason = plan.stop
                return
            if plan.relaunch_for is not None:
                if not self._relaunch_toward(plan.relaunch_for):
                    return
                continue
            decision = plan.decision
            assert decision is not None
            if decision.kind in ("done", "give_up"):
                self._note(decision.kind, "passed", decision.rationale or plan.source)
                self.narrative.append(decision.kind)
                self.stop_reason = decision.kind
                return
            if not self._step(decision, plan.source, plan.control, navigation=plan.navigation):
                return

    def _budget_exhausted(self) -> str:
        if self.steps_used >= self.settings.max_steps:
            return "max_steps"
        if self.clock() - self.started >= self.settings.max_wall_time_s:
            return "max_wall_time"
        return ""

    def _launch(self) -> bool:
        self.narrative.append("launch")
        self.replay.append({"action": "launch"})
        try:
            self.driver.launch()
        except AppCrashedError as exc:
            self._crash_on_launch(str(exc))
            return False
        except AppMissingError as exc:
            self._launch_failed("App failed to launch", f"App bundle missing, could not launch: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - any launch failure is an error result
            self._launch_failed("App failed to launch", f"{type(exc).__name__}: {exc}")
            return False
        self._note("launch", "passed", "launched")
        try:
            meta = self.driver.metadata()
            self.environment = {
                "path": meta.path or self.config.app.path or "",
                "bundle_id": meta.bundle_id or self.config.app.bundle_id or "",
                "version": meta.version or "",
                "platform": self.platform,
            }
        except Exception:  # noqa: BLE001
            self.environment = {"platform": self.platform}
        return True

    def _launch_failed(self, title: str, details: str) -> None:
        self._note("launch", "failed", details)
        self.fatal = details
        issue = CheckIssue(
            kind="launch",
            category="crash",
            title=title,
            severity="critical",
            details=details,
            check="agent_loop",
        )
        self._record_issue(issue, None)

    def _crash_on_launch(self, message: str) -> None:
        self._note("launch", "failed", message)
        self.fatal = f"app crashed on launch: {message}"
        issue = CheckIssue(
            kind="crash",
            category="crash",
            title="Crash on launch",
            severity="critical",
            details=f"App crashed on launch: {message}",
            check="agent_loop",
        )
        self._record_issue(issue, None)

    def _maybe_start_video(self) -> None:
        try:
            record = should_start_video(self.config.video.mode, self.shard.kind)
        except ValueError:
            return
        if not record:
            return
        try:
            self.driver.start_video()
        except Exception:  # noqa: BLE001 - video is best effort
            return
        self.video_started = True

    def _stop_video(self) -> Path | None:
        if not self.video_started:
            return None
        try:
            path = self.driver.stop_video()
        except Exception:  # noqa: BLE001
            path = getattr(self.driver, "video_path", None)
        return Path(path) if path else None

    # Observation --------------------------------------------------------

    def _observe(self, *, screenshot: bool | None = None) -> ScreenObservation:
        self.observations += 1
        shot = self.settings.screenshot_every_step if screenshot is None else screenshot
        return self.driver.observe(f"step-{self.observations:04d}", screenshot=shot)

    def _observe_step(self) -> ScreenObservation:
        """Observe; add a screenshot for a screen seen for the first time.

        The screenshot is taken on its own so the tree (and so the fingerprint)
        stays the one just observed. AppCrashedError and UITimeoutError
        propagate to the caller, which blames the step that caused them.
        """
        obs = self._observe()
        if obs.screenshot is None and fingerprint(obs.tree) not in self.graph.nodes:
            shot = self.driver.screenshot(f"step-{self.observations:04d}")
            obs = replace(obs, screenshot=Path(shot))
        return obs

    def _observe_timed_out(
        self,
        message: str,
        *,
        before: ScreenObservation | None = None,
        action: StepDecision | None = None,
        since: float | None = None,
    ) -> bool:
        """Record an observation that timed out. Return False to end the session.

        Checks see `ctx.error` starting with ``timeout:`` and an empty tree, so a
        functional check can report a hang. The next loop turn observes again
        before deciding anything.
        """
        error = f"timeout: observe: {message}"
        self._note("observe", "failed", error)
        self.stale = True
        self.observe_timeouts += 1
        self.stall += 1
        self.fail_streak += 1
        ctx = StepContext(
            after=ScreenObservation(tree=[], ts=time.time()),
            driver=self.driver,
            before=before,
            action=action,
            since_ts=time.time() if since is None else since,
            screen_id="",
            step_index=self.steps_used,
            goal=self.goal,
            error=error,
        )
        self._run_checks(ctx, new_screen=False)
        if self.observe_timeouts >= max(1, self.settings.stall_limit):
            self.stop_reason = "unresponsive"
            return False
        return True

    def _refresh(self) -> bool:
        """Observe again after a timed-out observation. Return False to end the session."""
        before = self.current
        try:
            obs = self._observe_step()
        except UITimeoutError as exc:
            return self._observe_timed_out(str(exc), before=before)
        except AppCrashedError as exc:
            self._note("observe", "failed", f"crash: {exc}")
            if before is None:
                self._crash_on_launch(str(exc))
                return False
            return self._handle_crash(None, before, time.time(), str(exc) or "app crashed")
        self.stale = False
        self.observe_timeouts = 0
        if self.pending_edge is not None:
            # The action whose observation timed out still led here.
            source, action = self.pending_edge
            self.graph.add_edge(source, action, fingerprint(obs.tree))
            self.pending_edge = None
        self._visit(obs, before=before, action=None, since=obs.ts)
        return True

    def _visit(
        self,
        obs: ScreenObservation,
        *,
        before: ScreenObservation | None,
        action: StepDecision | None,
        since: float,
        error: str = "",
    ) -> None:
        screen_id = fingerprint(obs.tree)
        is_new = screen_id not in self.graph.nodes
        shot = self._public(obs.screenshot) if obs.screenshot is not None else None
        self.graph.add_screen(screen_id, actionable_controls(obs.tree), shot)
        self.current, self.current_id = obs, screen_id
        ctx = StepContext(
            after=obs,
            driver=self.driver,
            before=before,
            action=action,
            since_ts=since,
            screen_id=screen_id,
            step_index=self.steps_used,
            goal=self.goal,
            error=error,
        )
        self._run_checks(ctx, new_screen=is_new)

    # Checks and findings --------------------------------------------------

    def _run_checks(self, ctx: StepContext, *, new_screen: bool) -> list[CheckIssue]:
        issues: list[CheckIssue] = []
        for check in self.checks:
            if getattr(check, "per_screen", False) and not (new_screen and self.settings.per_screen_checks):
                continue
            try:
                found = list(check.run(ctx) or [])
            except Exception as exc:  # noqa: BLE001 - a broken check never stops the loop
                name = getattr(check, "name", type(check).__name__)
                self._note(f"check {name}", "failed", f"{type(exc).__name__}: {exc}")
                continue
            for issue in found:
                if not issue.check:
                    issue = replace(issue, check=getattr(check, "name", ""))
                issues.append(issue)
                self._record_issue(issue, ctx)
        return issues

    def _record_issue(self, issue: CheckIssue, ctx: StepContext | None) -> Finding | None:
        key = fingerprint_for(issue.kind, issue.title, issue.target())
        if key in self.seen:
            return None
        self.seen.add(key)
        if issue.screenshot is None and ctx is not None:
            shot = ctx.after.screenshot or (ctx.before.screenshot if ctx.before else None)
            if shot is not None:
                issue = replace(issue, screenshot=shot)
        finding = issue.to_finding(
            finding_id=f"f-{len(self.findings) + 1}",
            worker_id=self.worker_id,
            shard_id=self.shard.id,
            backend=self.backend,
            steps=list(self.narrative),
            media_root=self.campaign if self.relative else None,
            environment=dict(self.environment),
        )
        self.findings.append(finding)
        self.finding_replays.append([dict(step) for step in self.replay])
        return finding

    # Deciding -------------------------------------------------------------

    def _decide(self) -> _Plan:
        if self.mode == "crawl":
            if self.fail_streak >= self.settings.stall_limit:
                return _Plan(stop="stall")
            return self._crawl_plan()
        return self._goal_plan()

    def _goal_plan(self) -> _Plan:
        if self.stall >= self.settings.stall_limit:
            return _Plan(stop="stall")
        obs = self.current
        assert obs is not None
        picks = self._goal_candidates(obs.tree)
        if self.provider is None or self.settings.heuristic_first:
            confident = [e for e in picks if any(matches_name(e, name) for name in self.expected)]
            if len(confident) == 1 or (self.provider is None and picks):
                element = confident[0] if len(confident) == 1 else picks[0]
                return _Plan(
                    self._element_decision(element, "heuristic: matches the goal"),
                    "heuristic",
                    control=control_key(element),
                )
        if self.provider is not None and self.model_calls < self.settings.max_model_calls:
            decision = self._ask_model(obs)
            if decision is not None:
                return _Plan(decision, "model")
        if self.provider is None and goal_satisfied(obs.tree, self.expected) and not picks:
            return _Plan(StepDecision(kind="done", rationale="heuristic: expected controls present"), "heuristic")
        if picks:
            return _Plan(
                self._element_decision(picks[0], "fallback: shares goal words"),
                "heuristic",
                control=control_key(picks[0]),
            )
        plan = self._crawl_plan()
        if plan.stop:
            return _Plan(StepDecision(kind="give_up", rationale="nothing left to try"), "crawl")
        plan.source = f"fallback {plan.source}"
        return plan

    def _goal_candidates(self, tree: list[UIElement]) -> list[UIElement]:
        """Untried enabled tappable elements sharing a goal word, in tree order."""
        node = self.graph.nodes.get(self.current_id)
        tried = node.tried if node else set()
        found: list[UIElement] = []
        for element in walk(tree):
            if normalize_role(element.role) not in TAP_ROLES or not element.enabled:
                continue
            if control_key(element) in tried:
                continue
            names = [n for n in (element.label, element.identifier) if n]
            if any(shares(name, self.tokens) for name in names):
                found.append(element)
        return found

    def _element_decision(self, element: UIElement, rationale: str) -> StepDecision:
        query = ElementQuery(role=element.role, label=element.label or None, identifier=element.identifier)
        return StepDecision(kind="tap", target=query, rationale=rationale)

    def _ask_model(self, obs: ScreenObservation) -> StepDecision | None:
        assert self.provider is not None
        self.model_calls += 1
        history = self.history[-self.settings.history_limit :] if self.settings.history_limit else []
        try:
            decision = self.provider.decide_step(
                obs, self.goal, list(history), timeout_s=self.settings.model_timeout_s
            )
        except ModelError as exc:
            self._note("model", "failed", f"{type(exc).__name__}: {exc}; falling back")
            return None
        if decision.usage is not None:
            self.cost += decision.usage.cost
        problem = self._invalid(decision)
        if problem:
            self._note("model", "failed", f"unusable decision ({problem}); falling back")
            return None
        return decision

    @staticmethod
    def _invalid(decision: StepDecision) -> str:
        kind = decision.kind
        if kind in ("tap", "type") and decision.target is None:
            return f"{kind} without target"
        if kind == "tap_point" and decision.point is None:
            return "tap_point without point"
        if kind == "swipe" and (decision.point is None or decision.end is None):
            return "swipe without points"
        if kind == "key" and not decision.keys:
            return "key without keys"
        return ""

    def _crawl_plan(self) -> _Plan:
        present = {control_key(element) for element in walk(self.current.tree)} if self.current else set()
        for control in self.graph.untried(self.current_id):
            if control.key not in present:
                # Gone from this screen (for example "Delete 3 items" became
                # "Delete 2 items" under the same fingerprint): never retry it.
                self.graph.mark_tried(self.current_id, control.key)
                continue
            return _Plan(_control_decision(control, self.settings.sample_text), "crawl", control=control.key)
        nearest = self.graph.nearest_frontier(self.current_id, skip=self.unreachable)
        if nearest is not None:
            _, path = nearest
            decision = decision_from_json(path[0].action)
            decision.rationale = "navigate to frontier"
            return _Plan(decision, "navigate", navigation=True)
        frontier = [sid for sid in self.graph.untried_frontier() if sid not in self.unreachable]
        if not frontier:
            return _Plan(stop="frontier_empty")
        if self.current_id not in self.back_tried:
            self.back_tried.add(self.current_id)
            return _Plan(StepDecision(kind="back", rationale="look for a way back"), "navigate")
        target = frontier[0]
        if self.relaunches_for.get(target, 0) >= 1 or self.graph.path_to(target) is None:
            self.unreachable.add(target)
            return self._crawl_plan()
        return _Plan(relaunch_for=target, source="navigate")

    # Acting ---------------------------------------------------------------

    def _step(
        self,
        decision: StepDecision,
        source: str,
        control: str | None = None,
        *,
        navigation: bool = False,
    ) -> bool:
        """Do one action, observe, and check. Return False to end the session.

        `navigation` marks a step that follows a known graph edge; if it fails,
        the edge is dropped so the route is not retried.
        """
        before = self.current
        before_id = self.current_id
        assert before is not None
        since = time.time()
        self.steps_used += 1
        self._mark_tried(before, before_id, decision, control)
        text = describe(decision)
        crashed = False
        error = ""
        outcome = _Outcome()
        self.planned_replay = []
        effective = self._resolve(decision, before)
        if effective.kind == "tap_point" and decision.kind == "tap" and effective.point is not None:
            text = f"{text} (at {effective.point[0]:g}, {effective.point[1]:g} to reach the exact element)"
        try:
            outcome = self._execute(effective, before)
        except AppCrashedError as exc:
            crashed, error = True, str(exc) or "app crashed"
            outcome = _Outcome(replay=self._replay_for(effective) or self.planned_replay)
        except UITimeoutError as exc:
            error = f"timeout: {exc}"
            outcome = _Outcome(replay=self._replay_for(effective) or self.planned_replay)
        except (UnsupportedAction, ElementNotFoundError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        if outcome.note:
            text = f"{text} ({outcome.note})"
        self.narrative.append(text)
        self.replay.extend(outcome.replay)
        if not crashed:
            try:
                after = self._observe_step()
            except AppCrashedError as exc:
                crashed, error = True, str(exc) or "app crashed"
            except UITimeoutError as exc:
                self._note(text, "failed" if error else "passed", error or f"{source}: observe timed out")
                self.history.append(
                    HistoryStep(action=decision, screen=before_id, outcome="screen did not respond")
                )
                if not error:
                    self.pending_edge = (before_id, decision_to_json(decision))
                return self._observe_timed_out(str(exc), before=before, action=decision, since=since)
        if crashed:
            self._note(text, "failed", f"crash: {error}")
            self.history.append(HistoryStep(action=decision, screen=before_id, outcome="app crashed"))
            return self._handle_crash(decision, before, since, error)
        after_id = fingerprint(after.tree)
        if not error:
            self.graph.add_edge(before_id, decision_to_json(decision), after_id)
        elif navigation:
            # A known route that no longer works must not be retried forever.
            self.graph.drop_edge(before_id, decision_to_json(decision))
        self._note(text, "failed" if error else "passed", error or f"{source}: {after_id}")
        changed = after_id != before_id
        self.stall = 0 if changed and not error else self.stall + 1
        self.fail_streak = self.fail_streak + 1 if error else 0
        if error:
            summary = f"failed: {error}"
        else:
            summary = f"screen changed to {after_id}" if changed else "no visible change"
        self.history.append(HistoryStep(action=decision, screen=before_id, outcome=summary))
        self._visit(after, before=before, action=decision, since=since, error=error)
        return True

    def _mark_tried(
        self,
        before: ScreenObservation,
        screen_id: str,
        decision: StepDecision,
        control: str | None,
    ) -> None:
        """Mark the control this step is for, by exact identity, whether or not it works."""
        if control is not None:
            self.graph.mark_tried(screen_id, control)
            return
        if decision.target is None:
            return
        element = exact_element(before.tree, decision.target)
        if element is not None:
            self.graph.mark_tried(screen_id, control_key(element))

    @staticmethod
    def _resolve(decision: StepDecision, before: ScreenObservation) -> StepDecision:
        """The action that reaches exactly the element `decision` means.

        A tap whose label query would hit another element first becomes a query
        narrowed by identifier, or a `tap_point` at the element's centre. The
        result is what the driver runs and what the replay records.
        """
        if decision.kind not in ("tap", "type") or decision.target is None:
            return decision
        query, point = precise_target(before.tree, decision.target)
        if decision.kind == "tap" and point is not None:
            return replace(decision, kind="tap_point", target=None, point=point)
        return replace(decision, target=query)

    def _execute(self, decision: StepDecision, before: ScreenObservation) -> _Outcome:
        kind = decision.kind
        driver = self.driver
        if kind == "tap":
            if decision.target is None:
                raise UnsupportedAction("tap without a target")
            driver.click(decision.target)
        elif kind == "type":
            if decision.target is None:
                raise UnsupportedAction("type without a target")
            driver.type_text(decision.target, decision.text or "")
        elif kind == "tap_point":
            if decision.point is None:
                raise UnsupportedAction("tap_point without a point")
            driver.tap_point(*decision.point)
        elif kind == "swipe":
            if decision.point is None or decision.end is None:
                raise UnsupportedAction("swipe without points")
            driver.swipe(decision.point, decision.end)
        elif kind == "key":
            if not decision.keys:
                raise UnsupportedAction("key without keys")
            driver.keychord(list(decision.keys))
        elif kind == "back":
            return self._back(before)
        else:
            raise UnsupportedAction(f"cannot execute {kind}")
        return _Outcome(replay=self._replay_for(decision))

    def _back(self, before: ScreenObservation) -> _Outcome:
        """Go back one screen.

        Both platforms first tap a visible Back button (a tappable element whose
        label or identifier is back/done/close/cancel, preferring one inside a
        navigation bar). Otherwise macOS presses escape and iOS swipes in from
        the left edge.
        """
        button = self._back_button(before.tree)
        if button is not None:
            query = ElementQuery(role=button.role, label=button.label or None, identifier=button.identifier)
            # "Close" must not press an earlier "Close Account".
            query, point = precise_target(before.tree, query)
            note = f'via "{_query_text(query)}"'
            if point is not None:
                self.planned_replay = [{"action": "tap_point", "point": [float(point[0]), float(point[1])]}]
                self.driver.tap_point(*point)
            else:
                self.planned_replay = [{"action": "click", "target": _query_dict(query)}]
                self.driver.click(query)
            return _Outcome(replay=self.planned_replay, note=note)
        if self.platform == "ios":
            width, height = before.size or _DEFAULT_SIZE
            start, end = (1.0, height / 2), (width * 0.7, height / 2)
            self.planned_replay = [{"action": "swipe", "point": list(start), "end": list(end)}]
            self.driver.swipe(start, end)
            return _Outcome(replay=self.planned_replay, note="edge swipe")
        self.planned_replay = [{"action": "key", "keys": ["escape"]}]
        self.driver.keychord(["escape"])
        return _Outcome(replay=self.planned_replay, note="escape")

    @staticmethod
    def _back_button(tree: list[UIElement]) -> UIElement | None:
        best: UIElement | None = None

        def visit(nodes: list[UIElement], in_nav: bool) -> UIElement | None:
            nonlocal best
            for element in nodes:
                role = normalize_role(element.role)
                names = {(element.label or "").strip().lower(), (element.identifier or "").strip().lower()}
                if role in TAP_ROLES and element.enabled and names & _BACK_NAMES:
                    if in_nav:
                        return element
                    best = best or element
                found = visit(element.children, in_nav or role == "navigationbar")
                if found is not None:
                    return found
            return None

        return visit(tree, False) or best

    @staticmethod
    def _replay_for(decision: StepDecision) -> list[dict]:
        """Version-1 flow steps for `decision`; empty when the flow format cannot express it."""
        kind = decision.kind
        if kind == "tap" and decision.target is not None:
            return [{"action": "click", "target": _query_dict(decision.target)}]
        if kind == "type" and decision.target is not None:
            return [{"action": "type", "target": _query_dict(decision.target), "text": decision.text or ""}]
        if kind == "key" and decision.keys:
            return [{"action": "key", "keys": list(decision.keys)}]
        if kind == "tap_point" and decision.point is not None:
            return [{"action": "tap_point", "point": [float(decision.point[0]), float(decision.point[1])]}]
        if kind == "swipe" and decision.point is not None and decision.end is not None:
            return [
                {
                    "action": "swipe",
                    "point": [float(decision.point[0]), float(decision.point[1])],
                    "end": [float(decision.end[0]), float(decision.end[1])],
                }
            ]
        return []

    def _handle_crash(
        self, decision: StepDecision | None, before: ScreenObservation, since: float, error: str
    ) -> bool:
        """Run the checks on a crash, report it if no check did, and relaunch.

        `decision` is None when the app died while the loop was only observing.
        """
        self.crashes += 1
        after = ScreenObservation(tree=[], ts=time.time())
        ctx = StepContext(
            after=after,
            driver=self.driver,
            before=before,
            action=decision,
            since_ts=since,
            screen_id="",
            step_index=self.steps_used,
            goal=self.goal,
            crashed=True,
            error=error,
        )
        issues = self._run_checks(ctx, new_screen=False)
        if self.settings.report_unchecked_crashes and not any(i.kind == "crash" for i in issues):
            if decision is None:
                title, details, element = "Crash while idle", f"The app crashed between actions. {error}", None
            else:
                label = _query_text(decision.target) or describe(decision)
                title = f"Crash on {label}"
                details = f"The app crashed after: {describe(decision)}. {error}"
                element = decision.target
            self._record_issue(
                CheckIssue(
                    kind="crash",
                    category="crash",
                    title=title,
                    severity="critical",
                    details=details.strip(),
                    element=element,
                    check="agent_loop",
                ),
                ctx,
            )
        if self.crashes > self.settings.max_crash_relaunches:
            self.stop_reason = "too_many_crashes"
            return False
        if self._budget_exhausted():
            self.stop_reason = self._budget_exhausted()
            return False
        return self._relaunch("relaunch after crash")

    def _relaunch(self, why: str) -> bool:
        self.steps_used += 1
        self.narrative.append("relaunch")
        self.replay.append({"action": "relaunch"})
        self.pending_edge = None
        before = self.current
        since = time.time()
        try:
            self.driver.relaunch()
            obs = self._observe_step()
        except UITimeoutError as exc:
            self._note("relaunch", "passed", why)
            self.stall = 0
            return self._observe_timed_out(str(exc), before=before, since=since)
        except (AppCrashedError, AppMissingError) as exc:
            self._note("relaunch", "failed", str(exc))
            self.fatal = f"relaunch failed: {exc}"
            self.stop_reason = "relaunch_failed"
            return False
        self._note("relaunch", "passed", why)
        self.stall = 0
        self._visit(obs, before=before, action=None, since=since)
        return True

    def _relaunch_toward(self, target: str) -> bool:
        self.relaunches_for[target] = self.relaunches_for.get(target, 0) + 1
        return self._relaunch(f"relaunch to reach {target}")

    # Wrapping up --------------------------------------------------------

    def _note(self, action: str, status: str, message: str) -> None:
        self.results.append(StepResult(index=len(self.results), action=action, status=status, message=message))  # type: ignore[arg-type]

    def _public(self, path: Path | str) -> str:
        path = Path(path)
        if self.relative:
            try:
                return path.resolve().relative_to(self.campaign.resolve()).as_posix()
            except ValueError:
                pass
        return str(path)

    def status(self) -> str:
        if self.fatal and self.stop_reason != "relaunch_failed":
            return "error"
        if any(not f.advisory for f in self.findings):
            return "failed"
        if self.fatal:
            return "error"
        return "passed"

    def finish(self, started_at: str, started_mono: float) -> WorkerResult:
        if self.stop_reason:
            self._note("stop", "passed", self.stop_reason)
        status = self.status()
        video = self._stop_video()
        if video is not None and not keep_video(self.config.video.mode, self.shard.kind, status):
            try:
                video.unlink(missing_ok=True)
            except OSError:
                pass
            video = None
        if video is not None and not video.is_file():
            video = None
        public_video = self._public(video) if video is not None else None
        for finding, steps in zip(self.findings, self.finding_replays, strict=True):
            finding.video = public_video
            replay_path = write_replay(finding.id, self.campaign, steps)
            finding.replay_json = self._public(replay_path)
            write_finding(finding, self.campaign)
        try:
            self.graph.save(self.work_dir / "screen_graph.json")
        except OSError as exc:
            self._note("screen_graph", "failed", str(exc))
        return WorkerResult(
            worker_id=self.worker_id,
            shard_id=self.shard.id,
            status=status,  # type: ignore[arg-type]
            findings=list(self.findings),
            steps=list(self.results),
            started_at=started_at,
            finished_at=_iso_now(),
            error=self.fatal,
            estimated_cost=self.cost,
            worker_minutes=max(0.0, time.monotonic() - started_mono) / 60.0,
            backend=self.backend,
            shard_name=self.shard.name,
            shard_kind=self.shard.kind,
        )


def run_agent_loop(
    shard: Shard,
    driver,
    config: CampaignConfig,
    *,
    provider: ModelProvider | None = None,
    checks: Iterable[Check] = (),
    settings: AgentLoopSettings | None = None,
    work_dir: Path,
    worker_id: str,
    backend: str = "local",
    clock: Callable[[], float] = time.monotonic,
) -> WorkerResult:
    """Explore `shard` with an observe-decide-act loop and return the worker result.

    The screen graph is saved to `<work_dir>/screen_graph.json`. Findings and
    their replay flows go to `<campaign>/findings/`. The caller owns the
    driver and closes it.
    """
    started_at = _iso_now()
    started_mono = time.monotonic()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    loop = _AgentLoop(
        shard,
        driver,
        config,
        provider=provider,
        checks=checks,
        settings=settings or AgentLoopSettings.from_config(config),
        work_dir=work_dir,
        worker_id=worker_id,
        backend=backend,
        clock=clock,
    )
    try:
        loop.run()
    except Exception as exc:  # noqa: BLE001 - an internal error is an error result, not a crash
        loop.fatal = f"{type(exc).__name__}: {exc}"
        loop.stop_reason = loop.stop_reason or "internal_error"
    return loop.finish(started_at, started_mono)
