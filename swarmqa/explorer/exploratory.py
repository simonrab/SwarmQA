"""C5 — goal-directed exploration inside step and time budgets.

See docs/CONTRACTS.md section C5 and docs/exploratory.md.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from swarmqa.decision import build_evaluator
from swarmqa.decision.heuristic import (
    MENU_ROLES as _MENU_ROLES,
    STOPWORDS as _STOPWORDS,
    absent_expected,
    click_key,
    click_key_from_query,
    element_names as _element_names,
    goal_satisfied,
    matches_name as _matches_name,
    menu_paths as _menu_paths,
    parse_goal as _parse_goal,
    query_for as _query_for,
    role_kind as _role_kind,
    shares as _shares,
    words as _words,
)
from swarmqa.decision.protocol import DecisionAction, DecisionEvaluator, Observation
from swarmqa.decision.serialize_tree import summarize_tree
from swarmqa.driver.query import walk
from swarmqa.errors import (
    AppCrashedError,
    AppMissingError,
    ChunkNotReady,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.friction.emit import metrics_for, maybe_emit_friction, resolve_allow_ratio
from swarmqa.friction.gold import resolve_gold, scripted_steps_from_shard
from swarmqa.friction.session import FrictionSession
from swarmqa.models import (
    CampaignConfig,
    ElementQuery,
    Finding,
    Shard,
    StepResult,
    UIElement,
    WorkerResult,
)
from swarmqa.serialize import dump_json
from swarmqa.video_policy import keep_video, should_start_video
from swarmqa.visual.judge import apply_judgment

_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "issue.md"


def _monotonic() -> float:
    """Clock seam. Tests may replace this; production uses time.monotonic."""
    return time.monotonic()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fault_text(element: UIElement) -> tuple[str, str] | None:
    """Return ``(display, reason)`` when a label or value contains error or empty."""
    for text in (element.label, element.value or ""):
        lowered = (text or "").lower()
        if "error" in lowered:
            return (element.label or text, "error")
        if "empty" in lowered:
            return (element.label or text, "empty")
    return None


def _target_dict(element: UIElement) -> dict:
    payload: dict[str, str] = {"role": element.role}
    if element.label:
        payload["label"] = element.label
    if element.identifier:
        payload["identifier"] = element.identifier
    return payload


def _fingerprint(kind: str, title: str, target: str) -> str:
    try:
        from swarmqa.reporter.findings import fingerprint_for

        return fingerprint_for(kind, title, target)
    except ChunkNotReady:
        payload = f"{kind}\n{title.strip().lower()}\n{target.strip().lower()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _campaign_dir(work_dir: Path) -> Path:
    resolved = work_dir.resolve()
    if resolved.parent.name == "workers":
        return resolved.parent.parent
    return resolved


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _render_markdown(finding: Finding) -> str:
    if _TEMPLATE.is_file():
        template = _TEMPLATE.read_text(encoding="utf-8")
    else:
        template = (
            "# {{title}}\n\n"
            "Severity: {{severity}}\n"
            "Kind: {{kind}}\n"
            "Fingerprint: {{fingerprint}}\n\n"
            "Worker: {{worker_id}}\n"
            "Backend: {{backend}}\n\n"
            "## Steps\n\n{{steps}}\n\n"
            "## Evidence\n\n"
            "Video: {{video}}\n\n"
            "Screenshots:\n\n{{screenshots}}\n\n"
            "Replay JSON: {{replay_json}}\n\n"
            "## Details\n\n{{details}}\n"
        )
    environment = "\n".join(
        f"{key}: {value}" for key, value in finding.environment.items()
    )
    values = {
        "title": finding.title,
        "severity": finding.severity,
        "kind": finding.kind,
        "steps": "\n".join(finding.steps),
        "video": finding.video or "",
        "screenshots": "\n".join(finding.screenshots),
        "replay_json": finding.replay_json or "",
        "environment": environment,
        "worker_id": finding.worker_id,
        "backend": finding.backend,
        "build_id": finding.environment.get("version", ""),
        "fingerprint": finding.fingerprint,
        "details": finding.details,
    }

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)

    return re.sub(r"\{\{([a-z0-9_]+)\}\}", replace, template)


def _write_replay_file(finding_id: str, campaign_dir: Path, steps: list[dict], name: str) -> Path:
    try:
        from swarmqa.reporter.findings import write_replay

        return write_replay(finding_id, campaign_dir, steps)
    except ChunkNotReady:
        path = campaign_dir / "findings" / f"{finding_id}.replay.json"
        dump_json({"version": 1, "name": name or "exploratory", "steps": steps}, path)
        return path


def _write_finding_file(finding: Finding, campaign_dir: Path) -> Path:
    try:
        from swarmqa.reporter.findings import write_finding

        return write_finding(finding, campaign_dir)
    except ChunkNotReady:
        path = campaign_dir / "findings" / f"{finding.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(finding), encoding="utf-8")
        return path


class _Budget:
    """Stops the hunt when explorer.max_steps or explorer.max_time_s is spent."""

    def __init__(self, max_steps: int, max_time_s: float):
        self.max_steps = max_steps
        self.max_time_s = max_time_s
        self.used = 0
        self.started = _monotonic()

    def consume(self) -> bool:
        if self.used >= self.max_steps:
            return False
        if _monotonic() - self.started >= self.max_time_s:
            return False
        self.used += 1
        return True

    @property
    def steps_left(self) -> int:
        return max(0, self.max_steps - self.used)


class _Session:
    def __init__(
        self,
        shard: Shard,
        driver,
        config: CampaignConfig,
        *,
        worker_id: str,
        work_dir: Path,
        evaluator: DecisionEvaluator | None = None,
    ):
        self.shard = shard
        self.driver = driver
        self.config = config
        self.worker_id = worker_id
        self.work_dir = work_dir
        self.campaign = _campaign_dir(work_dir)
        goal_text = (shard.goal or "").strip() or (shard.name or "").strip()
        self.goal = goal_text
        self.tokens, self.expected = _parse_goal(goal_text)
        self.budget = _Budget(config.explorer.max_steps, config.explorer.max_time_s)
        self.evaluator = evaluator if evaluator is not None else build_evaluator(config)
        self.findings: list[Finding] = []
        self.replays: list[list[dict]] = []
        self.steps: list[StepResult] = []
        self.performed: list[dict] = []
        self.narrative: list[str] = []
        self.environment: dict[str, str] = {
            "path": config.app.path or "",
            "bundle_id": config.app.bundle_id or "",
            "version": "",
        }
        self.video_started = False
        self.abort = False
        self.fatal: str | None = None
        self.judge_note: str | None = None
        self._reported_missing: set[str] = set()
        self._tried_clicks: set[str] = set()
        self._tried_menus: set[tuple[str, ...]] = set()
        self._stall_count = 0
        self.friction: FrictionSession | None = None
        if config.explorer.friction.enabled:
            personas = config.explorer.friction.personas or ["expert"]
            self.friction = FrictionSession(persona=personas[0])

    def hunt(self) -> None:
        if not self._launch():
            return
        self._maybe_start_video()
        if self.abort:
            return
        tree = self._read_tree()
        if tree is None or self.abort:
            return
        self._scan_faults(tree)
        if self.abort:
            return
        # Observe once (budget already spent on the tree read), then decide→act
        # until the evaluator is done. Re-reading after every action would change
        # step accounting relative to the pre-decision hunt.
        while not self.abort:
            action = self.evaluator.decide(self._observation(tree))
            if action.kind in ("done", "noop"):
                self._mark_goal_reached(tree)
                return
            if action.kind == "missing":
                self._emit_missing(tree)
                return
            if action.kind == "click":
                if not self._act_click(tree, action):
                    return
                continue
            if action.kind == "menu":
                if not self._act_menu(action):
                    return
                continue
            return

    def finish(self, started_at: str, started_mono: float) -> WorkerResult:
        self._mark_goal_reached_on_finish()
        self._maybe_emit_friction()
        video_path = self._stop_video()
        status = self._status()
        if video_path is not None and not keep_video(
            self.config.video.mode, self.shard.kind, status
        ):
            try:
                video_path.unlink(missing_ok=True)
            except OSError:
                pass
            video_path = None
        if video_path is not None:
            relative = _relative(video_path, self.campaign)
            for finding in self.findings:
                finding.video = relative
        self._persist()
        elapsed = max(0.0, time.monotonic() - started_mono)
        return WorkerResult(
            worker_id=self.worker_id,
            shard_id=self.shard.id,
            status=status,
            findings=list(self.findings),
            steps=list(self.steps),
            started_at=started_at,
            finished_at=_iso_now(),
            error=self.fatal or self.judge_note,
            worker_minutes=elapsed / 60.0,
            backend=self.config.backend,
            shard_name=self.shard.name,
            shard_kind=self.shard.kind,
        )

    def _mark_goal_reached(self, tree: list[UIElement] | None) -> None:
        """Set friction.goal_reached when done/noop and the goal looks satisfied."""
        if self.friction is None or self.friction.goal_reached:
            return
        if any(item.kind == "missing_control" for item in self.findings):
            return
        if tree is not None and self.expected and goal_satisfied(tree, self.expected):
            self.friction.goal_reached = True
            return
        # done/noop after at least one tree observe / act, with no missing finding.
        if self.friction.steps_observed > 0:
            self.friction.goal_reached = True

    def _mark_goal_reached_on_finish(self) -> None:
        if self.friction is None or self.friction.goal_reached:
            return
        if self.fatal:
            return
        if any(
            item.kind in ("crash", "missing_control", "launch", "unresponsive")
            for item in self.findings
        ):
            return
        # Passed-looking hunt with progress and no functional findings.
        if not self.findings and self.friction.steps_observed > 0:
            self.friction.goal_reached = True

    def _maybe_emit_friction(self) -> None:
        if self.friction is None:
            return
        friction_cfg = self.config.explorer.friction
        scripted = scripted_steps_from_shard(self.shard)
        gold = resolve_gold(
            scripted_steps=scripted,
            expected_controls=len(self.expected),
        )
        intent_id = self.shard.id or self.shard.name or ""
        tags = list(self.shard.tags or [])
        allow_ratio = resolve_allow_ratio(
            friction_cfg, intent_id=intent_id, tags=tags
        )
        finding = maybe_emit_friction(
            session=self.friction,
            config=friction_cfg,
            gold=gold,
            expected_controls=len(self.expected),
            worker_id=self.worker_id,
            backend=self.config.backend,
            shard_id=self.shard.id,
            finding_id=f"f-{self.worker_id}-{len(self.findings) + 1}",
            steps=list(self.narrative),
            environment=dict(self.environment),
            intent_id=intent_id,
            locus=self.goal,
            tags=tags,
            allow_ratio=allow_ratio,
            fingerprint_fn=_fingerprint,
        )
        if finding is None:
            return
        self.findings.append(finding)
        self.replays.append([dict(step) for step in self.performed])

    def _friction_observe(
        self,
        tree: list[UIElement] | None,
        action_kind: str,
        target_key: str | None,
        success: bool,
    ) -> None:
        if self.friction is None or tree is None:
            return
        self.friction.observe(tree, action_kind, target_key, success)

    def _peek_tree(self) -> list[UIElement] | None:
        """Read the accessibility tree without consuming the hunt budget."""
        try:
            return list(self.driver.accessibility_tree())
        except Exception:
            return None

    def _friction_score_hint(self) -> int | None:
        if self.friction is None or self.friction.steps_observed <= 0:
            return None
        scripted = scripted_steps_from_shard(self.shard)
        gold = resolve_gold(
            scripted_steps=scripted,
            expected_controls=len(self.expected),
        )
        metrics = metrics_for(
            self.friction, gold, klm=self.config.explorer.friction.klm
        )
        return int(metrics["score"])

    def _observation(self, tree: list[UIElement]) -> Observation:
        persona = self.friction.persona.name if self.friction is not None else None
        return Observation(
            goal=self.goal,
            tokens=list(self.tokens),
            expected=list(self.expected),
            elements=tree,
            maturity=self.config.app.maturity,
            stall_count=self._stall_count,
            steps_left=self.budget.steps_left,
            tried_clicks=frozenset(self._tried_clicks),
            tried_menus=frozenset(self._tried_menus),
            tree_summary=summarize_tree(tree),
            persona=persona,
            friction_score_hint=self._friction_score_hint(),
        )

    def _act_click(self, tree: list[UIElement], action: DecisionAction) -> bool:
        query = action.query
        if query is None:
            self._stall_count += 1
            return True
        key = click_key_from_query(query)
        self._tried_clicks.add(key)
        element = self._find_element(tree, query)
        if element is None:
            self._stall_count += 1
            return True
        if not self.budget.consume():
            return False
        return self._click(element)

    def _act_menu(self, action: DecisionAction) -> bool:
        path = list(action.menu_path or [])
        if not path:
            self._stall_count += 1
            return True
        self._tried_menus.add(tuple(path))
        if not self.budget.consume():
            return False
        return self._select_menu(path)

    def _find_element(self, tree: list[UIElement], query: ElementQuery) -> UIElement | None:
        for element in walk(tree):
            if click_key(element) == click_key_from_query(query):
                return element
        for element in walk(tree):
            if query.role and element.role.lower() != query.role.lower():
                continue
            if query.label and query.label.lower() not in (element.label or "").lower():
                continue
            if query.identifier is not None and element.identifier != query.identifier:
                continue
            return element
        return None

    def _status(self) -> str:
        if self.fatal and not self.findings:
            return "error"
        if self.findings:
            return "failed"
        return "passed"

    def _launch(self) -> bool:
        if not self.budget.consume():
            return False
        self.narrative.append("launch")
        self.performed.append({"action": "launch"})
        try:
            self.driver.launch()
        except AppMissingError as exc:
            self._note("launch", "failed", str(exc))
            self._add_finding(
                kind="launch",
                title="App failed to launch",
                severity="critical",
                target=self.config.app.path or self.config.app.bundle_id or "",
                details="App bundle missing — could not launch",
            )
            return False
        except AppCrashedError as exc:
            self._note("launch", "failed", str(exc))
            self._add_finding(
                kind="crash",
                title="Crash on launch",
                severity="critical",
                target=self.config.app.path or "",
                details="App crashed on launch",
            )
            return False
        self._note("launch", "passed", "launched")
        self._capture_environment()
        return True

    def _maybe_start_video(self) -> None:
        try:
            record = should_start_video(self.config.video.mode, self.shard.kind)
        except ValueError as exc:
            self.fatal = str(exc)
            self.abort = True
            return
        if not record:
            return
        try:
            self.driver.start_video()
        except Exception:
            return
        self.video_started = True

    def _stop_video(self) -> Path | None:
        if not self.video_started:
            return None
        try:
            path = self.driver.stop_video()
        except (AppCrashedError, AppMissingError, UITimeoutError, OSError):
            path = getattr(self.driver, "video_path", None)
        if path is None:
            return None
        return Path(path)

    def _read_tree(self) -> list[UIElement] | None:
        if not self.budget.consume():
            return None
        try:
            tree = list(self.driver.accessibility_tree())
        except AppCrashedError as exc:
            self._note("search", "failed", str(exc))
            self._add_finding(
                kind="crash",
                title="Crash while reading the accessibility tree",
                severity="critical",
                target="accessibility tree",
                details="Accessibility tree crashed the app",
            )
            self._stop_for_policy()
            return None
        except UITimeoutError as exc:
            self._note("search", "failed", str(exc))
            self._add_finding(
                kind="unresponsive",
                title="Unresponsive accessibility tree",
                severity="high",
                target="accessibility tree",
                details="Accessibility tree timed out — control unresponsive",
            )
            self._stop_for_policy()
            return None
        self.narrative.append("search accessibility tree")
        self._note("search", "passed", f"goal words: {', '.join(self.tokens) or 'none'}")
        self._friction_observe(tree, "search", None, True)
        return tree

    def _scan_faults(self, tree: list[UIElement]) -> None:
        for element in walk(tree):
            fault = _fault_text(element)
            if not fault:
                continue
            display, reason = fault
            self._add_finding(
                kind="error_state",
                title=f"Error state: {display}",
                severity="medium",
                target=display,
                details=f"{display} shows an {reason} state",
            )

    def _click(self, element: UIElement) -> bool:
        label = element.label or element.identifier or element.role
        self.narrative.append(f"click {label}")
        self.performed.append(
            {"action": "click", "target": _target_dict(element)}
        )
        try:
            self.driver.click(_query_for(element))
        except AppCrashedError as exc:
            self._note("click", "failed", str(exc))
            self._add_finding(
                kind="crash",
                title=f"Crash on {label}",
                severity="critical",
                target=label,
                details=f"{label} crashed the app",
            )
            self._friction_observe(self._peek_tree(), "click", click_key(element), False)
            return not self._stop_for_policy()
        except UITimeoutError as exc:
            self._note("click", "failed", str(exc))
            self._add_finding(
                kind="unresponsive",
                title=f"Unresponsive control {label}",
                severity="high",
                target=label,
                details=f"{label} timed out — control unresponsive",
            )
            self._friction_observe(self._peek_tree(), "click", click_key(element), False)
            return not self._stop_for_policy()
        except ElementNotFoundError as exc:
            self._note("click", "failed", str(exc))
            if self.config.app.maturity == "prototype":
                self._add_missing([label])
            self._friction_observe(self._peek_tree(), "click", click_key(element), False)
            return not self._stop_for_policy()
        self._note("click", "passed", label)
        self._stall_count = 0
        self._friction_observe(self._peek_tree(), "click", click_key(element), True)
        return True

    def _select_menu(self, path: list[str]) -> bool:
        label = " > ".join(path)
        self.narrative.append(f"menu {label}")
        self.performed.append({"action": "menu", "path": list(path)})
        try:
            self.driver.select_menu(path)
        except AppCrashedError as exc:
            self._note("menu", "failed", str(exc))
            self._add_finding(
                kind="crash",
                title=f"Crash on {label}",
                severity="critical",
                target=path[-1] if path else label,
                details=f"{label} crashed the app",
            )
            self._friction_observe(self._peek_tree(), "menu", label, False)
            return not self._stop_for_policy()
        except UITimeoutError as exc:
            self._note("menu", "failed", str(exc))
            self._add_finding(
                kind="unresponsive",
                title=f"Unresponsive menu {label}",
                severity="high",
                target=path[-1] if path else label,
                details=f"{label} timed out — control unresponsive",
            )
            self._friction_observe(self._peek_tree(), "menu", label, False)
            return not self._stop_for_policy()
        self._note("menu", "passed", label)
        self._stall_count = 0
        self._friction_observe(self._peek_tree(), "menu", label, True)
        return True

    def _emit_missing(self, tree: list[UIElement]) -> None:
        if self.config.app.maturity != "prototype":
            return
        absent = [
            name
            for name in absent_expected(tree, self.expected)
            if name.strip().lower() not in self._reported_missing
        ]
        if absent:
            self._add_missing(absent)

    def _add_missing(self, names: list[str]) -> None:
        fresh = [name for name in names if name.strip().lower() not in self._reported_missing]
        if not fresh:
            return
        for name in fresh:
            self._reported_missing.add(name.strip().lower())
        phrase = " ".join(fresh)
        listed = ", ".join(fresh)
        title = f"Missing control: {listed}" if len(fresh) == 1 else f"Missing controls: {listed}"
        severity = "high" if self.config.app.maturity == "prototype" else "medium"
        self._add_finding(
            kind="missing_control",
            title=title,
            severity=severity,
            target=listed,
            details=f"{phrase} missing — trying menu bar",
        )

    def _add_finding(
        self,
        *,
        kind: str,
        title: str,
        severity: str,
        target: str,
        details: str,
    ) -> None:
        finding_id = f"f-{self.worker_id}-{len(self.findings) + 1}"
        screenshots: list[str] = []
        shot = self._screenshot(finding_id)
        if shot:
            screenshots.append(shot)
        finding = Finding(
            id=finding_id,
            title=title,
            severity=severity,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            steps=list(self.narrative),
            fingerprint=_fingerprint(kind, title, target),
            worker_id=self.worker_id,
            backend=self.config.backend,
            shard_id=self.shard.id,
            screenshots=screenshots,
            environment=dict(self.environment),
            details=details,
        )
        self.findings.append(finding)
        self.replays.append([dict(step) for step in self.performed])

    def _screenshot(self, name: str) -> str | None:
        if not getattr(self.driver, "launched", False):
            return None
        if not self.budget.consume():
            return None
        try:
            path = Path(self.driver.screenshot(name))
        except (AppCrashedError, AppMissingError, UITimeoutError, ElementNotFoundError, OSError):
            self._note("screenshot", "failed", name)
            return None
        self.narrative.append(f"screenshot {name}")
        self.performed.append({"action": "screenshot", "name": name})
        self._note("screenshot", "passed", name)
        public = _relative(path, self.campaign)
        if self.config.visual.judgment.enabled:
            self._judge_screenshot(path, public, name)
        return public

    def _judge_screenshot(self, path: Path, public: str, name: str) -> None:
        finding, noted = apply_judgment(
            path,
            self.config,
            worker_id=self.worker_id,
            shard_id=self.shard.id,
            backend=self.config.backend,
            error=self.judge_note,
            name=name,
            evidence=public,
            steps=list(self.narrative),
            environment=dict(self.environment),
        )
        self.judge_note = noted
        if finding is None:
            return
        self.findings.append(finding)
        self.replays.append([dict(step) for step in self.performed])

    def _capture_environment(self) -> None:
        try:
            meta = self.driver.metadata()
        except Exception:
            return
        self.environment = {
            "path": meta.path or self.config.app.path or "",
            "bundle_id": meta.bundle_id or self.config.app.bundle_id or "",
            "version": meta.version or "",
        }

    def _stop_for_policy(self) -> bool:
        """Return True when the failure policy ends the hunt."""
        if self.config.explorer.on_step_failure == "stop":
            self.abort = True
            return True
        return False

    def _note(self, action: str, status: str, message: str) -> None:
        self.steps.append(
            StepResult(
                index=len(self.steps),
                action=action,
                status=status,  # type: ignore[arg-type]
                message=message,
            )
        )

    def _persist(self) -> None:
        flow_name = self.shard.name or self.shard.goal or "exploratory"
        for finding, steps in zip(self.findings, self.replays, strict=True):
            replay_path = _write_replay_file(finding.id, self.campaign, steps, flow_name)
            finding.replay_json = _relative(replay_path, self.campaign)
            _write_finding_file(finding, self.campaign)


def run_exploratory(
    shard: Shard,
    driver,
    config: CampaignConfig,
    *,
    worker_id: str,
    work_dir: Path,
    evaluator: DecisionEvaluator | None = None,
) -> WorkerResult:
    """Hunt within explorer.max_steps and explorer.max_time_s.

    The loop is observe → decide → act. The default heuristic evaluator
    preserves the classic strategy order: search the accessibility tree,
    click enabled buttons that share a goal word, try matching menu paths,
    then emit ``missing_control`` on prototype builds when an expected
    control is still absent.
    """
    started_at = _iso_now()
    started_mono = time.monotonic()
    session = _Session(
        shard,
        driver,
        config,
        worker_id=worker_id,
        work_dir=Path(work_dir),
        evaluator=evaluator,
    )
    try:
        session.hunt()
    except AppCrashedError as exc:
        session._add_finding(
            kind="crash",
            title="Crash during exploration",
            severity="critical",
            target="",
            details=f"App crashed during exploration — {exc}",
        )
    except UITimeoutError as exc:
        session._add_finding(
            kind="unresponsive",
            title="Unresponsive during exploration",
            severity="high",
            target="",
            details=f"UI timed out during exploration — {exc}",
        )
    except AppMissingError as exc:
        session._add_finding(
            kind="launch",
            title="App failed to launch",
            severity="critical",
            target=config.app.path or "",
            details=f"App bundle missing — {exc}",
        )
    except Exception as exc:
        session.fatal = f"{type(exc).__name__}: {exc}"
    return session.finish(started_at, started_mono)
