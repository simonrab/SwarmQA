"""C5 — goal-directed exploration inside step and time budgets.

See docs/CONTRACTS.md section C5 and docs/exploratory.md.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from swarmqa.driver.query import walk
from swarmqa.errors import (
    AppCrashedError,
    AppMissingError,
    ChunkNotReady,
    ElementNotFoundError,
    UITimeoutError,
)
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

# Glue words and common verbs. Remaining words are treated as control names
# when the goal does not quote a control.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "and",
        "or",
        "in",
        "on",
        "for",
        "with",
        "from",
        "into",
        "via",
        "near",
        "within",
        "without",
        "over",
        "under",
        "open",
        "click",
        "press",
        "tap",
        "find",
        "check",
        "verify",
        "confirm",
        "that",
        "this",
        "these",
        "those",
        "is",
        "be",
        "are",
        "was",
        "were",
        "button",
        "buttons",
        "menu",
        "menus",
        "control",
        "controls",
        "try",
        "using",
        "use",
        "app",
        "when",
        "then",
        "should",
        "can",
        "it",
        "its",
        "by",
        "at",
        "as",
        "if",
        "we",
        "user",
        "please",
        "just",
        "any",
        "all",
        "show",
        "see",
        "look",
        "around",
        "else",
        "broken",
        "toggle",
        "turn",
        "switch",
        "enable",
        "disable",
        "make",
        "sure",
        "does",
        "did",
        "not",
        "dont",
        "stay",
        "finish",
        "area",
        "window",
        "title",
        "updates",
        "update",
        "panel",
        "screen",
        "page",
        "view",
        "item",
        "once",
        "after",
        "before",
        "while",
        "have",
        "has",
        "had",
        "will",
        "would",
        "could",
        "about",
        "there",
        "their",
        "them",
        "they",
        "you",
        "your",
        "our",
        "out",
        "off",
        "how",
        "what",
        "which",
        "who",
        "where",
        "why",
        "also",
        "only",
        "than",
        "per",
        "etc",
        "something",
        "anything",
        "everything",
        "nothing",
        "here",
        "back",
        "again",
        "still",
        "already",
        "another",
        "other",
        "explore",
        "search",
        "navigate",
        "reach",
        "visit",
        "ensure",
        "validate",
        "given",
        "scenario",
        "label",
        "field",
        "text",
    }
)
_MENU_ROLES = frozenset({"menu", "menuitem", "menubaritem", "menubar", "menu bar"})
_QUOTE = re.compile(r'"([^"]+)"|\'([^\']+)\'')
_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "templates" / "issue.md"
)


def _monotonic() -> float:
    """Clock seam. Tests may replace this; production uses time.monotonic."""
    return time.monotonic()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _words(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9]+", text or "")}


def _shares(text: str, tokens: list[str]) -> bool:
    words = _words(text)
    return any(token.lower() in words for token in tokens)


def _parse_goal(text: str) -> tuple[list[str], list[str]]:
    """Return search tokens and the control names the goal expects to find.

    Quoted phrases are the expected controls. Otherwise every significant
    word is an expected control name (``Settings gear`` → Settings, gear).
    """
    quoted: list[str] = []
    for match in _QUOTE.finditer(text or ""):
        phrase = (match.group(1) or match.group(2) or "").strip()
        if phrase:
            quoted.append(phrase)
    seen: set[str] = set()
    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", (text or "").replace("'", "")):
        key = word.lower()
        if len(key) < 3 or key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        tokens.append(word)
    for phrase in quoted:
        for word in re.findall(r"[A-Za-z0-9]+", phrase.replace("'", "")):
            key = word.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(word)
    expected = quoted if quoted else list(tokens)
    return tokens, expected


def _element_names(element: UIElement) -> list[str]:
    names: list[str] = []
    if element.label:
        names.append(element.label)
    if element.identifier:
        names.append(element.identifier)
    return names


def _matches_name(element: UIElement, name: str) -> bool:
    target = name.strip()
    if not target:
        return False
    for label in _element_names(element):
        if " " in target:
            if target.lower() in label.lower() or _words(target) <= _words(label):
                return True
            continue
        if target.lower() in _words(label):
            return True
    return False


def _role_kind(role: str) -> str:
    lowered = role.lower()
    if lowered == "button":
        return "button"
    if lowered in _MENU_ROLES:
        return "menu"
    return "other"


def _fault_text(element: UIElement) -> tuple[str, str] | None:
    """Return ``(display, reason)`` when a label or value contains error or empty."""
    for text in (element.label, element.value or ""):
        lowered = (text or "").lower()
        if "error" in lowered:
            return (element.label or text, "error")
        if "empty" in lowered:
            return (element.label or text, "empty")
    return None


def _query_for(element: UIElement) -> ElementQuery:
    return ElementQuery(
        role=element.role,
        label=element.label or None,
        identifier=element.identifier,
    )


def _target_dict(element: UIElement) -> dict:
    payload: dict[str, str] = {"role": element.role}
    if element.label:
        payload["label"] = element.label
    if element.identifier:
        payload["identifier"] = element.identifier
    return payload


def _menu_paths(elements: list[UIElement], tokens: list[str]) -> list[list[str]]:
    found: list[list[str]] = []

    def walk_menus(nodes: list[UIElement], prefix: list[str]) -> None:
        for element in nodes:
            role = element.role.lower()
            label = (element.label or "").strip()
            path = prefix
            if role in _MENU_ROLES and label:
                candidate = prefix + [label]
                if _shares(label, tokens) or any(
                    _matches_name(element, token) for token in tokens
                ):
                    found.append(candidate)
                if role in {"menu", "menuitem", "menubaritem"}:
                    path = candidate
            child_prefix = path if role in {"menubar", "menu bar", "menu"} else prefix
            if element.children:
                walk_menus(element.children, child_prefix)

    walk_menus(elements, [])
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for path in found:
        key = tuple(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


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


class _Session:
    def __init__(
        self,
        shard: Shard,
        driver,
        config: CampaignConfig,
        *,
        worker_id: str,
        work_dir: Path,
    ):
        self.shard = shard
        self.driver = driver
        self.config = config
        self.worker_id = worker_id
        self.work_dir = work_dir
        self.campaign = _campaign_dir(work_dir)
        goal_text = (shard.goal or "").strip() or (shard.name or "").strip()
        self.tokens, self.expected = _parse_goal(goal_text)
        self.budget = _Budget(config.explorer.max_steps, config.explorer.max_time_s)
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
        self._reported_missing: set[str] = set()

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
        if self.abort or not self._click_buttons(tree):
            return
        if self._goal_satisfied(tree):
            return
        if not self._try_menus(tree):
            return
        self._emit_missing(tree)

    def finish(self, started_at: str, started_mono: float) -> WorkerResult:
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
            error=self.fatal,
            worker_minutes=elapsed / 60.0,
            backend=self.config.backend,
            shard_name=self.shard.name,
            shard_kind=self.shard.kind,
        )

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

    def _click_buttons(self, tree: list[UIElement]) -> bool:
        for element in walk(tree):
            if element.role.lower() != "button" or not element.enabled:
                continue
            names = _element_names(element)
            if not any(_shares(name, self.tokens) for name in names):
                continue
            if not self.budget.consume():
                return False
            if not self._click(element):
                return False
        return True

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
            return not self._stop_for_policy()
        except ElementNotFoundError as exc:
            self._note("click", "failed", str(exc))
            if self.config.app.maturity == "prototype":
                self._add_missing([label])
            return not self._stop_for_policy()
        self._note("click", "passed", label)
        return True

    def _goal_satisfied(self, tree: list[UIElement]) -> bool:
        if not self.expected:
            return True
        for name in self.expected:
            kinds = {
                _role_kind(element.role)
                for element in walk(tree)
                if _matches_name(element, name)
            }
            if not kinds or kinds == {"menu"}:
                return False
        return True

    def _try_menus(self, tree: list[UIElement]) -> bool:
        paths = _menu_paths(tree, self.tokens)
        covered = {tuple(path) for path in paths}
        for name in self._absent(tree):
            synthetic = (name,)
            if synthetic in covered:
                continue
            if any(name.lower() == part.lower() for path in paths for part in path):
                continue
            paths.append([name])
            covered.add(synthetic)
        for path in paths:
            if not self.budget.consume():
                return False
            if not self._select_menu(path):
                return False
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
            return not self._stop_for_policy()
        self._note("menu", "passed", label)
        return True

    def _emit_missing(self, tree: list[UIElement]) -> None:
        if self.config.app.maturity != "prototype":
            return
        absent = [
            name
            for name in self._absent(tree)
            if name.strip().lower() not in self._reported_missing
        ]
        if absent:
            self._add_missing(absent)

    def _absent(self, tree: list[UIElement]) -> list[str]:
        missing: list[str] = []
        for name in self.expected:
            if any(_matches_name(element, name) for element in walk(tree)):
                continue
            missing.append(name)
        return missing

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
        finding_id = f"f-{len(self.findings) + 1}"
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
        return _relative(path, self.campaign)

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
) -> WorkerResult:
    """Hunt within explorer.max_steps and explorer.max_time_s.

    Strategies run in order until the goal controls are satisfied or the
    budget ends: search the accessibility tree, click enabled buttons that
    share a goal word, try matching menu paths, then emit ``missing_control``
    on prototype builds when an expected control is still absent.
    """
    started_at = _iso_now()
    started_mono = time.monotonic()
    session = _Session(
        shard,
        driver,
        config,
        worker_id=worker_id,
        work_dir=Path(work_dir),
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
