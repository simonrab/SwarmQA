"""WP-D1: intents proposed from a code change."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarmqa.errors import ConfigError
from swarmqa.flows.from_diff import (
    FlowsError,
    collect_change,
    intents_from_diff,
    propose,
    render_intent,
    write_intents,
)
from swarmqa.flows.settings import FlowsSettings
from swarmqa.intent.ingest import build_queue
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import FlowProposal, ProposedFlow
from swarmqa.models import CampaignConfig


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "App").mkdir(parents=True)
    (root / "AppTests").mkdir()
    (root / "App" / "ProfileView.swift").write_text('Button("Save") {}\n', encoding="utf-8")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "App" / "ProfileView.swift").write_text('Button("Save profile") {}\nTextField("Name")\n', encoding="utf-8")
    (root / "AppTests" / "ProfileTests.swift").write_text("func testX() {}\n", encoding="utf-8")
    (root / "README.md").write_text("changed\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "change")
    return root


def flow(name: str, priority: int, **kw) -> ProposedFlow:
    return ProposedFlow(name=name, goal=kw.pop("goal", f"Do {name}"), priority=priority, **kw)


def test_collect_change_keeps_ui_files_and_drops_tests_and_docs(repo):
    change = collect_change(repo, "main", "feature")
    assert set(change.changed) == {"App/ProfileView.swift", "AppTests/ProfileTests.swift", "README.md"}
    assert [f.path for f in change.files] == ["App/ProfileView.swift"]
    assert 'Button("Save profile")' in change.files[0].content
    assert "+TextField" in change.diff and "README" not in change.diff and not change.truncated


def test_collect_change_cuts_to_size(repo):
    settings = FlowsSettings(max_diff_bytes=40, max_file_bytes=10)
    change = collect_change(repo, "main", "feature", settings=settings)
    assert change.truncated and "[cut" in change.diff and "[cut" in change.files[0].content


def test_collect_change_reads_the_working_tree(repo):
    (repo / "App" / "ProfileView.swift").write_text('Text("Draft")\n', encoding="utf-8")
    change = collect_change(repo, "main", None)
    assert 'Text("Draft")' in change.files[0].content and "+Text" in change.diff


def test_collect_change_reports_a_bad_ref(repo):
    with pytest.raises(FlowsError, match="git diff"):
        collect_change(repo, "nope", "feature")


def test_nothing_ui_relevant_means_no_model_call(repo):
    git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("docs only\n", encoding="utf-8")
    git(repo, "commit", "-qam", "docs")
    provider = FakeModelProvider()
    assert propose(provider, collect_change(repo, "main~1", "main")) == []
    assert provider.calls == []


def test_propose_sorts_by_priority_drops_blank_and_duplicates(repo):
    proposal = FlowProposal(
        flows=[
            flow("Edit name", 3),
            flow("Save profile", 1),
            flow("save profile", 2),
            flow("Nameless", 2, goal=" "),
            flow("Way out", 9),
        ]
    )
    provider = FakeModelProvider(proposals=[proposal])
    flows = propose(provider, collect_change(repo, "main", "feature"), max_flows=10)
    assert [(f.name, f.priority) for f in flows] == [("Save profile", 1), ("Edit name", 3), ("Way out", 5)]


def test_written_intents_load_as_exploratory_shards_in_priority_order(tmp_path):
    flows = [
        flow("Save profile", 1, steps=["Open Profile", "Tap Save profile"], expected=["Saved banner"],
             touched_files=["App/ProfileView.swift"]),
        flow("Edit name", 2),
    ]
    out = tmp_path / "intents"
    paths = write_intents(flows, out)
    assert [p.name for p in paths] == ["01-p1-save-profile.md", "02-p2-edit-name.md"]
    config = CampaignConfig()
    config.intents = [str(out)]
    shards = build_queue(config)
    assert [s.name for s in shards] == ["Save profile", "Edit name"]
    first = shards[0]
    assert first.kind == "exploratory" and "Tap Save profile" in first.goal and "Saved banner" in first.goal
    assert "from-diff" in first.tags and "priority:1" in first.tags


def test_write_intents_replaces_only_generated_files(tmp_path):
    out = tmp_path / "intents"
    write_intents([flow("Old", 1)], out)
    (out / "mine.md").write_text("# Mine\n", encoding="utf-8")
    write_intents([flow("New", 1)], out)
    assert sorted(p.name for p in out.glob("*.md")) == ["01-p1-new.md", "mine.md"]


def test_render_intent_never_emits_a_steps_section():
    text = render_intent(flow("A", 1, steps=["click it"]))
    assert "## Steps" not in text and "## How to get there" in text


def test_intents_from_diff_end_to_end_and_needs_llm(repo, tmp_path):
    config = CampaignConfig()
    with pytest.raises(FlowsError, match=r"\[llm\]"):
        intents_from_diff(config, repo, "main", "feature", tmp_path / "out", log=lambda _: None)
    provider = FakeModelProvider(proposals=[FlowProposal(flows=[flow("Save profile", 1)])])
    lines: list[str] = []
    paths = intents_from_diff(config, repo, "main", "feature", tmp_path / "out", provider=provider, log=lines.append)
    assert len(paths) == 1 and "1 intent(s)" in lines[-1]


def test_flows_settings_reject_unknown_keys_and_bad_values():
    with pytest.raises(ConfigError) as info:
        FlowsSettings.from_mapping({"max_flow": 3, "max_files": 0})
    assert "flows.max_flow: unknown key" in info.value.errors
    assert "flows.max_files: must be >= 1" in info.value.errors


def test_flows_cli_propose_writes_intents(repo, tmp_path, monkeypatch, capsys):
    from swarmqa.flows import cli

    provider = FakeModelProvider(proposals=[FlowProposal(flows=[flow("Save profile", 1)])])
    monkeypatch.setattr("swarmqa.flows.from_diff.provider_for", lambda config: provider)
    out = tmp_path / "out"
    code = cli.main(["propose", "--base", "main", "--head", "feature", "--source", str(repo), "--out", str(out)])
    assert code == 0
    assert capsys.readouterr().out.strip() == str(out / "01-p1-save-profile.md")


def test_github_run_adds_intents_from_the_diff_once_per_event(tmp_path, monkeypatch):
    from swarmqa.github.events import Event
    from swarmqa.github.settings import GitHubSettings
    from swarmqa.github.watch import evaluate_sha
    from swarmqa.models import BuildArtifact, CampaignResult

    class Builder:
        def build_result(self, sha, platform, *, repo=None, force=False):
            from swarmqa.build.builder import BuildResult

            return BuildResult(platform, "a" * 40, artifact=BuildArtifact(platform, f"/b/{platform}.app", "dev.app"))

    config = CampaignConfig()
    config.report_root = str(tmp_path / "reports")
    config.flows.settings = {"from_diff": True}
    calls, seen = [], []

    def diff_intents(event, cfg, source, out):
        calls.append((event.base, out))
        out.mkdir(parents=True)
        (out / "01-p1-x.md").write_text("# X\n", encoding="utf-8")
        return [out / "01-p1-x.md"]

    def run_campaign(cfg, queue, *, options):
        seen.append(list(cfg.intents))
        return CampaignResult("c", str(tmp_path / "c"))

    monkeypatch.setattr("swarmqa.github.watch.use_gh_credentials", lambda env=None: None)
    event = Event("pr", "a" * 40, 7, base="main")
    evaluate_sha(event, config, GitHubSettings(platforms=["ios", "macos"]), repo="acme/app", builder=Builder(),
                 run_campaign=run_campaign, diff_intents=diff_intents, log=lambda _: None)
    assert len(calls) == 1 and calls[0][0] == "main"
    expected = str(Path(config.report_root) / "diff-intents" / ("a" * 12))
    assert seen == [[expected], [expected]]
    assert config.intents == []
