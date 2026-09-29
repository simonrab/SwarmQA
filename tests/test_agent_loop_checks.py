"""The agent loop running the real default checks on a fake app with planted bugs."""

from __future__ import annotations

from pathlib import Path

from swarmqa.checks import default_checks
from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.models import Shard, UIElement
from swarmqa.testing import make_app, sample_config


def _window(*children: UIElement) -> list[UIElement]:
    return [UIElement(role="window", label="Main", frame=(0, 0, 390, 844), children=list(children))]


def _home() -> list[UIElement]:
    return _window(
        UIElement(role="button", label="Open Settings", identifier="home.settings", frame=(20, 100, 200, 44)),
        UIElement(role="button", label="Refresh", identifier="home.refresh", frame=(20, 200, 200, 44)),
        UIElement(role="button", label="Crash Me", identifier="home.crash", frame=(20, 300, 200, 44)),
    )


def _settings() -> list[UIElement]:
    return _window(
        UIElement(role="button", label="Back", identifier="settings.back", frame=(0, 40, 60, 44)),
        UIElement(role="button", frame=(20, 200, 44, 44)),
    )


def test_agent_loop_with_default_checks_finds_planted_bugs(tmp_path: Path):
    driver = FakeDriver(make_app(tmp_path), tmp_path / "work")
    driver.set_tree(_home())
    driver.transitions = {"Open Settings": _settings(), "Back": _home()}
    driver.crash_labels = {"Crash Me"}

    result = run_agent_loop(
        Shard(id="s1", kind="exploratory", name="crawl"),
        driver,
        sample_config(make_app(tmp_path)),
        checks=default_checks(),
        settings=AgentLoopSettings(mode="crawl", max_steps=40),
        work_dir=tmp_path / "campaign" / "workers" / "w1",
        worker_id="w1",
    )

    by_kind = {(finding.kind, finding.title) for finding in result.findings}
    titles = " | ".join(sorted(title for _, title in by_kind))
    assert result.status == "failed"
    # Refresh does nothing: a dead tap.
    assert any(kind == "unresponsive" and "Refresh" in title for kind, title in by_kind), titles
    # Crash Me crashes the app: one crash finding, not two.
    crashes = [f for f in result.findings if f.kind == "crash"]
    assert len(crashes) == 1 and crashes[0].category == "crash", titles
    # The settings button has no label.
    assert any(f.category == "broken" and "label" in f.title.lower() for f in result.findings), titles
    for finding in result.findings:
        assert finding.replay_json
