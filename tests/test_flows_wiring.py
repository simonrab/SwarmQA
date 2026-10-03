"""Lead wiring for Wave D: diff flows and the replay cache in the queue and the GitHub trigger."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmqa.flows import FlowPlan
from swarmqa.flows.cache import CachedFlow, FlowCache, cache_root, shard_key
from swarmqa.github.events import Event
from swarmqa.github.watch import _diff_flows
from swarmqa.intent.ingest import build_queue
from swarmqa.models import Shard
from swarmqa.testing import sample_config

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

VIEW = """import SwiftUI

struct ProfileView: View {
    var body: some View {
        VStack {
%s        }
    }
}
"""


def _config(tmp_path: Path, **flows):
    intents = tmp_path / "intents"
    intents.mkdir(exist_ok=True)
    (intents / "home.md").write_text("# Browse home\n\nLook around the home screen.\n")
    config = sample_config()
    config.intents = [str(intents)]
    config.app.source_dir = str(tmp_path)
    config.flows.settings = {"cache": True, **flows}
    return config


def test_the_cache_is_off_by_default(tmp_path):
    from swarmqa.flows.settings import FlowsSettings

    assert FlowsSettings().cache is False


def test_extra_shards_go_first_and_get_ids(tmp_path):
    config = _config(tmp_path)
    diff = Shard(id="", kind="exploratory", name="Profile view", goal='Open Profile and use "Save".')
    queue = build_queue(config, extra=[diff])
    assert [s.name for s in queue] == ["Profile view", "Browse home"]
    assert all(s.id for s in queue)


def test_a_cached_goal_becomes_a_scripted_replay(tmp_path):
    config = _config(tmp_path)
    shard = Shard(id="", kind="exploratory", name="Profile view", goal='Open Profile and use "Save".')
    cache = FlowCache(cache_root(config))
    cache.save(CachedFlow(key=shard_key(shard), name=shard.name, goal=shard.goal,
                          steps=[{"action": "click", "target": {"label": "Save"}}]))
    first = build_queue(config, extra=[shard])[0]
    assert first.kind == "scripted" and "cached" in first.tags
    assert build_queue(_config(tmp_path, cache=False), extra=[shard])[0].kind == "exploratory"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
                          ).stdout.strip()


@needs_git
def test_diff_flows_diffs_the_pr_against_its_base(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "ProfileView.swift").write_text(VIEW % "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "ProfileView.swift").write_text(VIEW % '            Button("Save") {}\n')
    _git(repo, "commit", "-qam", "head")
    head = _git(repo, "rev-parse", "HEAD")
    builder = SimpleNamespace(repo_cache=lambda source: SimpleNamespace(mirror=repo))
    logs: list[str] = []
    config = _config(tmp_path)
    plan = _diff_flows(Event("pr", head, 1, base=base), config, builder, None, logs.append)
    assert plan.source == "heuristic"
    assert [s.name for s in plan.shards] == ["Profile view"]
    assert '"Save"' in plan.shards[0].goal
    # A push has no base: the parent is used.
    assert _diff_flows(Event("push", head), config, builder, None, logs.append).shards
    assert any("1 flow(s) from the diff" in line for line in logs)


def test_diff_flows_never_fails_the_run(tmp_path):
    def broken(source):
        raise RuntimeError("no mirror")

    logs: list[str] = []
    plan = _diff_flows(Event("pr", "abc", 1, base="def"), _config(tmp_path),
                       SimpleNamespace(repo_cache=broken), None, logs.append)
    assert plan == FlowPlan()
    assert "no flows from the diff" in logs[0]
    off = _diff_flows(Event("pr", "abc", 1), _config(tmp_path, from_diff=False),
                      SimpleNamespace(repo_cache=broken), None, logs.append)
    assert off == FlowPlan() and len(logs) == 1


def test_a_goal_reached_in_the_backend_is_replayed_from_the_cache_next_time(tmp_path):
    from swarmqa.backends.local import execute_shard
    from tests.test_agent_loop import config_for, make_driver

    config = config_for(tmp_path)
    config.explorer.engine = "agent"
    config.app.source_dir = str(tmp_path)
    config.flows.settings = {"cache": True}
    config.checks.settings = {"functional": False}  # the fake switch is dead on purpose
    goal = 'toggle "Dark Mode" in Settings'
    shard = Shard(id="s1", kind="exploratory", name="version", goal=goal)
    drivers = []

    def factory(target, work_dir):
        drivers.append(make_driver(tmp_path))
        return drivers[-1]

    first = execute_shard(shard, "w1", tmp_path / "c" / "workers" / "w1", config, driver_factory=factory,
                          campaign_root=tmp_path / "c")
    assert first.status == "passed"
    [cached] = cached_shards_for(config, shard)
    assert cached.kind == "scripted"
    second = execute_shard(cached, "w2", tmp_path / "c" / "workers" / "w2", config, driver_factory=factory,
                           campaign_root=tmp_path / "c")
    assert second.status == "passed"
    assert [e for e in drivers[1].actions_log if e.startswith("click:")] == ["click:Settings", "click:Dark Mode"]


def cached_shards_for(config, shard):
    from swarmqa.flows.cache import cached_shards

    return cached_shards(config, [shard])
