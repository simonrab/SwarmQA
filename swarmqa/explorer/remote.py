"""Let a coding agent in a chat (Claude Code, Codex) drive agent-loop sessions.

The agent loop asks its provider for every step in `mode="driven"`. Here the
provider is a `RemoteController`: `decide_step` publishes the current screen
to a `RemoteSession` and blocks until the agent sends an action through the
MCP tools (`swarmqa.mcp.tools`). The loop still launches the app, runs every
check after every step, records findings with screenshots and replays, and
writes the campaign report, so a session driven from a chat leaves the same
evidence as `aqa run`.

Sessions live in one process-wide `RemoteHub`. Each worker of a driven
campaign (one per simulator) builds its own controller through
`create_provider` (`[llm] provider = "chat"`), which opens a session in the
hub. Several workers give the agent several simulators to drive in parallel.

Thread model: the loop's worker thread blocks in `decide_step`; MCP tool calls
come from other threads and hand commands over through a condition variable.
Reported issues are recorded on the worker thread, inside `decide_step`, so
the loop's state is only ever touched by its own thread.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from swarmqa.checks.protocol import CheckIssue
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm.parse import parse_step
from swarmqa.llm.protocol import (
    ChangedFile,
    FlowProposal,
    HistoryStep,
    ModelError,
    ModelRefused,
    Rubric,
    ScreenJudgment,
    StepDecision,
)
from swarmqa.llm.serialize import history_text, tree_text
from swarmqa.models import UIElement

SessionState = str  # "starting" | "ready" | "busy" | "finished"
SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("broken", "visual", "confusing", "crash")
# How long a worker waits for the agent's next action before ending the session.
DEFAULT_IDLE_TIMEOUT_S = 900.0


@dataclass
class _Command:
    kind: str  # "act" | "report" | "end"
    payload: Any = None
    reply: Any = None
    done: threading.Event = field(default_factory=threading.Event)
    error: str = ""


class RemoteSession:
    """One driven device. The agent's side calls `observe`/`act`/`report`/`end`; the loop's side calls `publish`/`next_command`."""

    def __init__(self, session_id: str, *, goal: str = "", worker_id: str = "", idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S):
        self.id = session_id
        self.goal = goal
        self.worker_id = worker_id
        self.campaign_id = ""
        self.device = ""
        self.idle_timeout_s = idle_timeout_s
        self.state: SessionState = "starting"
        self.stop_reason = ""
        self.steps = 0
        self._cond = threading.Condition()
        self._screen: dict[str, Any] | None = None
        self._elements: dict[str, UIElement] = {}
        self._version = 0
        self._command: _Command | None = None
        self.findings: list[dict[str, Any]] = []
        self._reported_upto = 0

    # Agent side ---------------------------------------------------------

    def observe(self, *, wait_s: float = 60.0, after_version: int = -1) -> dict[str, Any]:
        """The current screen, waiting up to `wait_s` for the first one (or for a newer one than `after_version`)."""
        deadline = time.monotonic() + wait_s
        with self._cond:
            while self.state != "finished" and (self._screen is None or self._version <= after_version):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return self._snapshot()

    def act(self, action: dict[str, Any], *, wait_s: float = 120.0) -> dict[str, Any]:
        """Do one action (`{"kind": "tap", "element_id": "e3"}` and so on); return the next screen."""
        with self._cond:
            if self.state == "finished":
                raise ModelError(f"session {self.id} has finished ({self.stop_reason or 'ended'})")
            if self._screen is None:
                raise ModelError(f"session {self.id} is still starting; call observe first")
            decision = parse_step(dict(action), self._elements)
            version = self._version
        command = self._send(_Command("act", decision), wait_s)
        if command.error:
            raise ModelError(command.error)
        return self.observe(wait_s=wait_s, after_version=version)

    def report(self, title: str, *, severity: str = "medium", category: str = "visual", details: str = "") -> dict[str, Any]:
        """Record an issue the agent saw on the current screen; it becomes an advisory finding with the screenshot."""
        if severity not in SEVERITIES:
            raise ModelError(f"severity must be one of {', '.join(SEVERITIES)}")
        if category not in CATEGORIES:
            raise ModelError(f"category must be one of {', '.join(CATEGORIES)}")
        if not title.strip():
            raise ModelError("title is required")
        issue = CheckIssue(
            kind="visual_judgment",
            category=category,  # type: ignore[arg-type]
            title=title.strip(),
            severity=severity,  # type: ignore[arg-type]
            confidence=0.9,
            advisory=True,
            details=details.strip(),
            check="chat",
        )
        command = self._send(_Command("report", issue), 30.0)
        if command.error:
            raise ModelError(command.error)
        return {"recorded": command.reply is not None, "finding_id": command.reply or ""}

    def end(self, reason: str = "done", *, wait_s: float = 60.0) -> dict[str, Any]:
        """Finish the session; the loop stops, and the campaign report is written once every session has ended."""
        with self._cond:
            if self.state == "finished":
                return self._snapshot()
        self._send(_Command("end", reason), wait_s)
        with self._cond:
            deadline = time.monotonic() + wait_s
            while self.state != "finished" and time.monotonic() < deadline:
                self._cond.wait(deadline - time.monotonic())
            return self._snapshot()

    def _send(self, command: _Command, wait_s: float) -> _Command:
        with self._cond:
            if self.state == "finished":
                command.error = f"session {self.id} has finished"
                return command
            if self._command is not None:
                command.error = f"session {self.id} is busy with another action"
                return command
            self._command = command
            self._cond.notify_all()
        if not command.done.wait(wait_s):
            command.error = f"session {self.id} did not answer within {wait_s:g}s"
        return command

    # Loop side ----------------------------------------------------------

    def publish(self, obs: ScreenObservation, history: list[HistoryStep], findings: list[dict[str, Any]]) -> None:
        text, elements = tree_text(obs.tree)
        with self._cond:
            new = findings[self._reported_upto :]
            self._reported_upto = len(findings)
            self.findings = list(findings)
            self._elements = elements
            self._version += 1
            self._screen = {
                "tree": text,
                "screenshot": str(obs.screenshot) if obs.screenshot is not None else None,
                "size": list(obs.size) if obs.size else None,
                "history": history_text(history, limit=10),
                "new_findings": new,
            }
            self.state = "ready"
            self._cond.notify_all()

    def next_command(self) -> _Command | None:
        """Block until the agent sends a command; None when it stayed idle past `idle_timeout_s`."""
        deadline = time.monotonic() + self.idle_timeout_s
        with self._cond:
            while self._command is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            command = self._command
            if command.kind == "act":
                self.state = "busy"
            return command

    def complete(self, command: _Command, reply: Any = None, error: str = "") -> None:
        with self._cond:
            command.reply, command.error = reply, error
            if self._command is command:
                self._command = None
            command.done.set()
            self._cond.notify_all()

    def finish(self, reason: str) -> None:
        with self._cond:
            self.state = "finished"
            self.stop_reason = self.stop_reason or reason
            pending = self._command
            self._command = None
            self._cond.notify_all()
        if pending is not None:
            pending.error = pending.error or f"session ended ({reason})"
            pending.done.set()

    def _snapshot(self) -> dict[str, Any]:
        base = {
            "session_id": self.id,
            "campaign_id": self.campaign_id,
            "device": self.device,
            "goal": self.goal,
            "state": self.state,
            "steps": self.steps,
            "findings": len(self.findings),
        }
        if self.state == "finished":
            base["stop_reason"] = self.stop_reason
        if self._screen is not None:
            base["version"] = self._version
            base.update(self._screen)
        return base


class RemoteHub:
    """The process-wide registry of driven sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, RemoteSession] = {}
        self._ids = itertools.count(1)
        # Campaigns started from the chat: campaign id -> goal and idle timeout for new sessions.
        self.pending: dict[str, dict[str, Any]] = {}

    def open(self, *, goal: str = "", worker_id: str = "", campaign_id: str = "", idle_timeout_s: float | None = None) -> RemoteSession:
        with self._lock:
            session_id = f"s{next(self._ids)}"
            session = RemoteSession(
                session_id,
                goal=goal,
                worker_id=worker_id,
                idle_timeout_s=idle_timeout_s if idle_timeout_s is not None else DEFAULT_IDLE_TIMEOUT_S,
            )
            session.campaign_id = campaign_id
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> RemoteSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ModelError(f"unknown session {session_id!r}; list_sessions shows the open ones")
        return session

    def sessions(self, campaign_id: str | None = None) -> list[RemoteSession]:
        with self._lock:
            found = list(self._sessions.values())
        return [s for s in found if campaign_id is None or s.campaign_id == campaign_id]


HUB = RemoteHub()


class RemoteController:
    """A `ModelProvider` whose step decisions come from a `RemoteSession`."""

    name = "chat"

    def __init__(self, hub: RemoteHub | None = None, *, campaign_id: str = "", worker_id: str = "", idle_timeout_s: float | None = None):
        self.hub = hub or HUB
        self.campaign_id = campaign_id
        self.worker_id = worker_id
        self.idle_timeout_s = idle_timeout_s
        self.session: RemoteSession | None = None
        self.loop: Any = None

    def bind(self, loop: Any) -> None:
        """Called by the agent loop when it starts; opens this worker's session."""
        self.loop = loop
        self.session = self.hub.open(
            goal=getattr(loop, "goal", ""),
            worker_id=getattr(loop, "worker_id", self.worker_id),
            campaign_id=self.campaign_id,
            idle_timeout_s=self.idle_timeout_s,
        )
        driver = getattr(loop, "driver", None)
        self.session.device = str(getattr(driver, "udid", "") or getattr(getattr(driver, "target", None), "simulator", "") or "")

    def decide_step(self, obs: ScreenObservation, goal: str, history: list[HistoryStep], *, timeout_s: float = 30.0) -> StepDecision:
        session = self.session
        if session is None:
            raise ModelError("the controller was not bound to an agent loop")
        session.steps = getattr(self.loop, "steps_used", session.steps)
        while True:
            session.publish(obs, history, self._findings())
            command = session.next_command()
            if command is None:
                session.finish("idle")
                raise ModelError(f"no action from the chat for {session.idle_timeout_s:g}s")
            if command.kind == "end":
                session.complete(command, reply=True)
                return StepDecision(kind="done", rationale=str(command.payload or "ended from the chat"))
            if command.kind == "report":
                finding = self.loop.record_external_issue(command.payload) if self.loop is not None else None
                session.complete(command, reply=finding.id if finding is not None else None)
                continue
            session.complete(command, reply=True)
            return command.payload

    def _findings(self) -> list[dict[str, Any]]:
        found = getattr(self.loop, "findings", []) or []
        return [
            {
                "id": f.id,
                "title": f.title,
                "severity": f.severity,
                "kind": f.kind,
                "advisory": bool(getattr(f, "advisory", False)),
            }
            for f in found
        ]

    def close(self, reason: str) -> None:
        if self.session is not None:
            self.session.finish(reason)

    # The rest of the provider protocol: a chat session judges screens itself.

    def judge_screen(self, obs: ScreenObservation, rubric: Rubric, *, timeout_s: float = 60.0) -> ScreenJudgment:
        return ScreenJudgment()

    def propose_flows(self, diff: str, files: list[ChangedFile], *, max_flows: int = 10, timeout_s: float = 120.0) -> FlowProposal:
        return FlowProposal()

    def computer_use(self, obs: ScreenObservation, instruction: str, *, timeout_s: float = 60.0) -> StepDecision:
        raise ModelRefused("the chat controller does not do computer use")
