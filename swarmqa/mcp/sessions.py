"""MCP tools for driving the app from a chat: Claude Code or Codex is the explorer.

`open_sessions` starts a campaign inside the MCP server process with one
exploratory shard per device and `[llm] provider = "chat"`, so each worker's
agent loop waits for the chat agent's actions (`swarmqa.explorer.remote`).
The agent then loops `observe` -> `act` on each session, calls `report_issue`
for problems it sees, and `end_session` when done. Every action still goes
through the campaign's checks, findings, screenshots, video and replays, and
the campaign report is written once every session has ended, so
`list_findings` and `get_finding` work on it like on any campaign.

With `pr` or `sha`, the commit is checked out and built first
(`swarmqa.build`), the same way `aqa run --github-sha` builds it.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from swarmqa.errors import AQAError
from swarmqa.explorer.remote import HUB, RemoteSession
from swarmqa.llm.protocol import ModelError

# Campaigns this server started from the chat: campaign id -> runner thread.
_THREADS: dict[str, threading.Thread] = {}
_ERRORS: dict[str, str] = {}


@dataclass
class SessionsHandle:
    campaign_id: str
    report_dir: str
    sessions: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


def open_sessions(
    goal: str,
    config_path: str = "aqa.config.toml",
    app: str | None = None,
    devices: int = 1,
    pr: int | None = None,
    sha: str | None = None,
    platform: Literal["macos", "ios"] | None = None,
    constraints: list[str] | None = None,
    max_steps: int = 200,
    max_minutes: int = 60,
    wait_s: float = 120.0,
) -> SessionsHandle:
    """Start `devices` app sessions that you drive: you choose every tap.

    Use this to QA an app (or a PR's build with `pr`/`sha`) yourself. Each
    session is one simulator. Then, per session: `observe` to see the screen
    (an accessibility tree with element ids like e3, plus a screenshot path
    you can open), `act` to tap/type/swipe/go back, `report_issue` for
    anything broken, ugly or confusing that the automatic checks missed, and
    `end_session` when finished. Automatic checks (crashes, dead taps, error
    alerts, layout, contrast) run after every action and their findings show
    up in `new_findings`. Simulators can take minutes to boot: if a session is
    still `starting`, call `observe` again.
    """
    from swarmqa.mcp import campaigns
    from swarmqa.models import RunOptions, Shard
    from swarmqa.orchestrator import campaign as orchestrator
    from swarmqa.report.layout import ensure_campaign_layout
    from swarmqa.util import new_campaign_id

    if devices < 1:
        raise AQAError("devices must be at least 1")
    if not goal.strip():
        raise AQAError("goal is required: say what to explore and what to look for")
    root = campaigns.project_root()
    request = campaigns.CampaignRequest(
        config_path=str(campaigns.resolve_path(config_path or campaigns.DEFAULT_CONFIG, root)),
        cwd=str(root),
        app=app,
        intents=[],
        platform=platform,
        workers=devices,
        sha=sha,
    )
    if not Path(request.config_path).is_file():
        raise AQAError(f"config not found: {request.config_path}; run `aqa init` or pass config_path")
    config = campaigns.load_request_config(request)
    if pr is not None or sha:
        _build_commit(config, pr=pr, sha=sha)
    config = _driven_config(config, devices=devices, max_steps=max_steps, max_minutes=max_minutes)
    text = goal.strip()
    if constraints:
        text += "\nConstraints:\n" + "\n".join(f"- {item}" for item in constraints)
    queue = [
        Shard(id=f"chat-{index + 1}", kind="exploratory", name=f"chat session {index + 1}", goal=text,
              constraints=list(constraints or []))
        for index in range(devices)
    ]
    report_root = Path(config.report_root)
    campaign_id = new_campaign_id()
    while (report_root / campaign_id).exists():
        campaign_id = new_campaign_id()
    campaign = (report_root / campaign_id).resolve()
    ensure_campaign_layout(campaign)
    campaigns.write_meta(
        campaign,
        {
            "campaign_id": campaign_id,
            "backend": config.backend,
            "workers": devices,
            "shards": len(queue),
            "sha": sha,
            "pr": pr,
            "driven": True,
            "request": request.to_plain(),
            "created_at": campaigns.now(),
        },
    )

    def run() -> None:
        try:
            orchestrator.run_campaign(config, queue, options=RunOptions(campaign_id=campaign_id))
        except Exception as exc:  # noqa: BLE001 - reported through list_sessions
            _ERRORS[campaign_id] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, name=f"aqa-chat-{campaign_id}", daemon=True)
    _THREADS[campaign_id] = thread
    thread.start()
    sessions = _wait_for_sessions(campaign_id, devices, wait_s)
    message = ""
    if len(sessions) < devices:
        message = (
            f"{len(sessions)} of {devices} sessions are up; simulators may still be booting. "
            "Call list_sessions or observe again."
        )
    if campaign_id in _ERRORS:
        message = f"the campaign failed: {_ERRORS[campaign_id]}"
    return SessionsHandle(
        campaign_id=campaign_id,
        report_dir=str(campaign),
        sessions=[_summary(s) for s in sessions],
        message=message,
    )


def list_sessions(campaign_id: str | None = None) -> list[dict[str, Any]]:
    """Sessions this server is running (all, or one campaign's), with their state and finding counts."""
    found = [_summary(s) for s in HUB.sessions(campaign_id)]
    if campaign_id and campaign_id in _ERRORS:
        found.append({"campaign_id": campaign_id, "state": "failed", "error": _ERRORS[campaign_id]})
    return found


def observe(session_id: str, wait_s: float = 60.0) -> dict[str, Any]:
    """The session's current screen: `tree` (element ids for `act`), `screenshot` path, `history`, `new_findings`.

    Waits up to `wait_s` for the first screen while the simulator boots.
    """
    return _call(lambda: HUB.get(session_id).observe(wait_s=wait_s))


def act(
    session_id: str,
    kind: Literal["tap", "tap_point", "type", "swipe", "key", "back"],
    element_id: str | None = None,
    text: str | None = None,
    x: float | None = None,
    y: float | None = None,
    end_x: float | None = None,
    end_y: float | None = None,
    keys: list[str] | None = None,
    rationale: str = "",
) -> dict[str, Any]:
    """Do one action and return the next screen (same shape as `observe`).

    tap: element_id. type: element_id and text. tap_point: x, y (points).
    swipe: x, y, end_x, end_y. key: keys (e.g. ["return"]). back: go back one screen.
    Use element ids from the latest observe; they change when the screen changes.
    """
    action: dict[str, Any] = {"kind": kind, "rationale": rationale}
    for name, value in (("element_id", element_id), ("text", text), ("x", x), ("y", y),
                        ("end_x", end_x), ("end_y", end_y), ("keys", keys)):
        if value is not None:
            action[name] = value
    return _call(lambda: HUB.get(session_id).act(action))


def report_issue(
    session_id: str,
    title: str,
    severity: Literal["critical", "high", "medium", "low"] = "medium",
    category: Literal["broken", "visual", "confusing", "crash"] = "visual",
    details: str = "",
) -> dict[str, Any]:
    """Record a problem you see on the current screen; it becomes a finding with this screen's screenshot.

    Report what a careful tester would: broken behaviour, wrong or misleading
    text, clipped or overlapping layout, confusing flows. Write the title as
    the defect ("Save button stays disabled after filling the form").
    """
    return _call(lambda: HUB.get(session_id).report(title, severity=severity, category=category, details=details))


def end_session(session_id: str, reason: str = "done") -> dict[str, Any]:
    """Finish a session. When every session of the campaign has ended, its report is written."""
    return _call(lambda: HUB.get(session_id).end(reason))


SESSION_TOOLS = [open_sessions, list_sessions, observe, act, report_issue, end_session]


# Helpers ---------------------------------------------------------------------


def _call(fn) -> Any:
    try:
        return fn()
    except ModelError as exc:
        raise AQAError(str(exc)) from exc


def _summary(session: RemoteSession) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "campaign_id": session.campaign_id,
        "device": session.device,
        "state": session.state,
        "steps": session.steps,
        "findings": len(session.findings),
        **({"stop_reason": session.stop_reason} if session.state == "finished" else {}),
    }


def _wait_for_sessions(campaign_id: str, devices: int, wait_s: float) -> list[RemoteSession]:
    deadline = time.monotonic() + wait_s
    while True:
        found = HUB.sessions(campaign_id)
        thread = _THREADS.get(campaign_id)
        if len(found) >= devices or campaign_id in _ERRORS or (thread is not None and not thread.is_alive()):
            return found
        if time.monotonic() >= deadline:
            return found
        time.sleep(0.5)


def _driven_config(config, *, devices: int, max_steps: int, max_minutes: int):
    """The campaign config for chat-driven sessions: agent engine, chat provider, one worker per device."""
    config = copy.deepcopy(config)
    config.explorer.engine = "agent"  # type: ignore[assignment]
    config.explorer.max_steps = int(max_steps)
    config.explorer.max_time_s = float(max_minutes) * 60.0
    config.workers = int(devices)
    config.llm.enabled = True
    settings = dict(config.llm.settings)
    settings["provider"] = "chat"
    settings.pop("step_model", None)
    config.llm.settings = settings
    # Wall-clock budget for the whole campaign: the sessions plus booting.
    config.budgets.max_wall_time_s = max(float(config.budgets.max_wall_time_s or 0), float(max_minutes) * 60.0 + 600.0)
    config.coverage.exploratory = False
    return config


def _build_commit(config, *, pr: int | None, sha: str | None) -> None:
    """Check out and build the PR head or `sha` for the config's platform; point `app.path` at the build."""
    from swarmqa.build.builder import ShaBuilder
    from swarmqa.build.settings import BuildSettings
    from swarmqa.github.client import GitHubClient
    from swarmqa.github.settings import GitHubSettings
    from swarmqa.github.watch import build_repo, resolve_repo, use_gh_credentials

    settings = GitHubSettings.from_config(config)
    repo = resolve_repo(config, settings)
    if pr is not None and not sha:
        if not repo:
            raise AQAError("cannot look up the PR: set github.repo in the config")
        sha = GitHubClient(repo).pull(int(pr)).head_sha
    assert sha
    source = build_repo(config, settings, repo)
    if source and source.startswith("https://github.com/"):
        use_gh_credentials()
    platform = config.app.platform or "ios"
    built = ShaBuilder(BuildSettings.from_config(config)).build_result(sha, platform, repo=source)
    if built.failure is not None or built.artifact is None:
        summary = built.failure.summary() if built.failure is not None else "no app was produced"
        raise AQAError(f"build of {sha[:12]} failed: {summary}")
    config.app.path = built.artifact.app_path
    config.app.bundle_id = built.artifact.bundle_id
