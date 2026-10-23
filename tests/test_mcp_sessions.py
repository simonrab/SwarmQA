"""Chat-driven sessions: a coding agent picks every step through the MCP tools."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from swarmqa.checks import FunctionalCheck
from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import AQAError
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.explorer.remote import RemoteController, RemoteHub
from swarmqa.mcp import campaigns, sessions
from swarmqa.models import Shard, UIElement
from swarmqa.testing import make_app, sample_config


def _home() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            frame=(0, 0, 390, 844),
            children=[
                UIElement(role="button", label="Settings", frame=(20, 100, 120, 44)),
                UIElement(role="button", label="Broken", frame=(20, 200, 120, 44)),
                UIElement(role="textfield", label="Name", frame=(20, 300, 300, 44)),
            ],
        )
    ]


def _settings_screen() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Settings",
            frame=(0, 0, 390, 844),
            children=[UIElement(role="button", label="Back", frame=(10, 50, 60, 44))],
        )
    ]


class DemoDriver(FakeDriver):
    def launch(self) -> None:
        super().launch()
        self.tree = _home()
        self.transitions = {"Settings": _settings_screen(), "Back": _home()}


def _element(screen: dict, label: str) -> str:
    for line in screen["tree"].splitlines():
        if f'"{label}"' in line:
            return line.split()[0]
    raise AssertionError(f"{label} not in {screen['tree']}")


def _start_loop(tmp_path: Path, hub: RemoteHub):
    app = make_app(tmp_path)
    driver = DemoDriver(app, tmp_path / "work")
    config = sample_config(app)
    controller = RemoteController(hub, campaign_id="c1", idle_timeout_s=10)
    box: dict = {}

    def run() -> None:
        box["result"] = run_agent_loop(
            Shard(id="s1", kind="exploratory", name="chat", goal="look around"),
            driver,
            config,
            provider=controller,
            checks=[FunctionalCheck()],
            settings=AgentLoopSettings(mode="driven", max_steps=20, max_wall_time_s=60),
            work_dir=tmp_path / "reports" / "c1" / "workers" / "w1",
            worker_id="w1",
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not hub.sessions("c1") and time.monotonic() < deadline:
        time.sleep(0.01)
    return hub.sessions("c1")[0], thread, box, driver


def test_the_chat_picks_every_step_and_checks_still_run(tmp_path: Path):
    hub = RemoteHub()
    session, thread, box, driver = _start_loop(tmp_path, hub)

    screen = session.observe(wait_s=10)
    assert screen["state"] == "ready" and screen["goal"] == "look around"
    assert '"Settings"' in screen["tree"]

    # A tap that does nothing: the dead-tap check reports it in new_findings.
    after = session.act({"kind": "tap", "element_id": _element(screen, "Broken")})
    assert [f["kind"] for f in after["new_findings"]] == ["unresponsive"]

    after = session.act({"kind": "type", "element_id": _element(after, "Name"), "text": "Ada"})
    assert "type:Name:Ada" in driver.actions_log

    after = session.act({"kind": "tap", "element_id": _element(after, "Settings")})
    assert '"Back"' in after["tree"] and "Broken" not in after["tree"]

    reported = session.report("Settings has no title", severity="low", category="confusing", details="Empty header")
    assert reported["recorded"] and reported["finding_id"]

    ended = session.end("finished exploring")
    thread.join(10)
    assert ended["state"] == "finished"
    result = box["result"]
    assert result.status != "error"
    kinds = {(f.kind, f.title) for f in result.findings}
    assert ("visual_judgment", "Settings has no title") in kinds
    chat = next(f for f in result.findings if f.kind == "visual_judgment")
    assert chat.advisory and chat.severity == "low" and chat.screenshots


def test_bad_actions_are_errors_the_chat_can_fix(tmp_path: Path):
    hub = RemoteHub()
    session, thread, _, _ = _start_loop(tmp_path, hub)
    session.observe(wait_s=10)
    from swarmqa.llm.protocol import ModelError

    with pytest.raises(ModelError, match="unknown element_id"):
        session.act({"kind": "tap", "element_id": "e999"})
    with pytest.raises(ModelError, match="severity"):
        session.report("x", severity="urgent")
    session.end()
    thread.join(10)


def test_session_ends_when_the_chat_goes_quiet(tmp_path: Path):
    hub = RemoteHub()
    app = make_app(tmp_path)
    controller = RemoteController(hub, campaign_id="c2", idle_timeout_s=0.2)
    result = run_agent_loop(
        Shard(id="s1", kind="exploratory", name="chat", goal="g"),
        DemoDriver(app, tmp_path / "work"),
        sample_config(app),
        provider=controller,
        settings=AgentLoopSettings(mode="driven", max_steps=5, max_wall_time_s=30),
        work_dir=tmp_path / "w",
        worker_id="w1",
    )
    assert result.steps[-1].message == "controller_gone"
    assert hub.sessions("c2")[0].state == "finished"


# Through the MCP tools ---------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    campaigns.configure(root=tmp_path)
    launch = FakeDriver.launch

    def demo_launch(self) -> None:
        launch(self)
        self.tree = _home()
        self.transitions = {"Settings": _settings_screen(), "Back": _home()}

    monkeypatch.setattr(FakeDriver, "launch", demo_launch)
    yield tmp_path
    campaigns.configure()


def _write_config(project: Path) -> None:
    make_app(project)
    (project / "aqa.config.toml").write_text(
        '[app]\npath = "Sample.app"\n\n[campaign]\nworkers = 1\n\n[driver]\nkind = "fake"\n\n'
        "[coverage]\nvisual = false\nexploratory = false\n\n"
        f"[suite]\ncommand = \"{sys.executable} -c 'raise SystemExit(0)'\"\n",
        encoding="utf-8",
    )


def test_open_sessions_drive_and_end_writes_the_campaign_report(project: Path):
    _write_config(project)
    handle = sessions.open_sessions("Explore the demo app", wait_s=20)
    assert len(handle.sessions) == 1, handle.message
    session_id = handle.sessions[0]["session_id"]

    screen = sessions.observe(session_id, wait_s=20)
    after = sessions.act(session_id, "tap", element_id=_element(screen, "Settings"))
    assert '"Back"' in after["tree"]
    sessions.report_issue(session_id, "Back button label is vague", severity="low", category="confusing")
    ended = sessions.end_session(session_id)
    assert ended["state"] == "finished"

    report = Path(handle.report_dir)
    deadline = time.monotonic() + 20
    while not (report / "findings.json").is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    data = json.loads((report / "findings.json").read_text(encoding="utf-8"))
    titles = [f["title"] for f in data["findings"]]
    assert "Back button label is vague" in titles
    assert (report / "summary.md").is_file()
    assert [s["state"] for s in sessions.list_sessions(handle.campaign_id)] == ["finished"]


def test_open_sessions_needs_a_goal(project: Path):
    _write_config(project)
    with pytest.raises(AQAError, match="goal is required"):
        sessions.open_sessions("  ")


def test_unknown_session_is_a_tool_error(project: Path):
    with pytest.raises(AQAError, match="unknown session"):
        sessions.observe("nope", wait_s=0)
