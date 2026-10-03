from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmqa.decision.heuristic import parse_goal
from swarmqa.flows import FLOW_TAG, Change
from swarmqa.flows.from_diff import (
    ChangeError,
    collect_change,
    flows_from_diff,
    goal_text,
    heuristic_flows,
)
from swarmqa.flows.settings import FlowsSettings
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import ChangedFile, FlowProposal, ModelTimeout, ProposedFlow, Usage
from swarmqa.testing import sample_config

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SETTINGS_V1 = """import SwiftUI

struct SettingsView: View {
    var body: some View {
        Form {
            Text("Settings")
        }
    }
}
"""

SETTINGS_V2 = """import SwiftUI

struct SettingsView: View {
    @State private var dark = false
    var body: some View {
        Form {
            Text("Settings")
            Toggle("Dark Mode", isOn: $dark)
                .accessibilityIdentifier("settings.darkMode")
            Button("Reset") { dark = false }
        }
    }
}

struct ProfileEditorView: View {
    var body: some View {
        NavigationLink("Change Avatar", destination: Text("Avatar"))
    }
}
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "qa@example.com")
    _git(root, "config", "user.name", "QA")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "Sources").mkdir()
    (root / "Sources" / "SettingsView.swift").write_text(SETTINGS_V1)
    (root / "Sources" / "Old.swift").write_text("struct Old {}\n")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "tag", "base")
    (root / "Sources" / "SettingsView.swift").write_text(SETTINGS_V2)
    (root / "Sources" / "Old.swift").unlink()
    (root / "README.md").write_text("hello\nworld\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    _git(root, "tag", "head")
    return root


def _plan_for(flows, **kwargs):
    provider = FakeModelProvider(proposals=[FlowProposal(flows=flows)])
    change = Change(base="a", head="b", diff="diff --git a/x b/x\n+x\n")
    return provider, flows_from_diff(sample_config(), change, provider=provider, **kwargs)


@needs_git
def test_collect_change_reads_diff_and_touched_swift_files(repo: Path):
    change = collect_change(repo, "base", "head")
    assert change.base == "base" and change.head == "head"
    assert "Toggle(\"Dark Mode\"" in change.diff
    assert "README.md" in change.diff
    assert not change.truncated
    by_path = {item.path: item.content for item in change.files}
    assert set(by_path) == {"Sources/SettingsView.swift", "Sources/Old.swift"}
    assert by_path["Sources/SettingsView.swift"] == SETTINGS_V2
    assert by_path["Sources/Old.swift"] == ""


@needs_git
def test_collect_change_truncates_diff_and_caps_files(repo: Path):
    settings = FlowsSettings(max_file_chars=20)
    change = collect_change(repo, "base", "head", max_chars=50, settings=settings)
    assert change.truncated
    assert len(change.diff) == 50
    content = {item.path: item.content for item in change.files}["Sources/SettingsView.swift"]
    assert content == SETTINGS_V2[:20]

    change = collect_change(repo, "base", "head", settings=FlowsSettings(max_diff_chars=60))
    assert change.truncated and len(change.diff) == 60


@needs_git
def test_collect_change_filters_files_by_glob(repo: Path):
    change = collect_change(repo, "base", "head", settings=FlowsSettings(file_globs=["*.md"]))
    assert [item.path for item in change.files] == ["README.md"]
    assert change.files[0].content == "hello\nworld\n"

    change = collect_change(repo, "base", "head", settings=FlowsSettings(file_globs=["Sources/Settings*"]))
    assert [item.path for item in change.files] == ["Sources/SettingsView.swift"]


@needs_git
def test_collect_change_heuristic_end_to_end(repo: Path):
    change = collect_change(repo, "base", "head")
    plan = flows_from_diff(sample_config(), change)
    assert plan.source == "heuristic"
    names = [shard.name for shard in plan.shards]
    assert names == ["Settings view", "Profile Editor view"]
    settings = plan.shards[0]
    assert settings.goal == 'Open Settings and use "Dark Mode", "settings.darkMode" and "Reset".'
    _, expected = parse_goal(settings.goal)
    assert expected == ["Dark Mode", "settings.darkMode", "Reset"]


@needs_git
def test_collect_change_raises_on_bad_revision(repo: Path):
    with pytest.raises(ChangeError):
        collect_change(repo, "nope", "head")


def test_collect_change_falls_back_to_two_dot_without_merge_base():
    calls: list[list[str]] = []

    def runner(args, **_):
        calls.append(args)
        if "a...b" in args and "--name-status" not in args:
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: no merge base")
        if "--name-status" in args:
            return SimpleNamespace(returncode=0, stdout="M\0App.swift\0", stderr="")
        if args[-1] == "b:App.swift":
            return SimpleNamespace(returncode=0, stdout="struct App {}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="diff --git a/App.swift b/App.swift\n", stderr="")

    change = collect_change("/repo", "a", "b", runner=runner)
    assert change.diff.startswith("diff --git")
    assert change.files == [ChangedFile(path="App.swift", content="struct App {}\n")]
    diff_specs = [args[-1] for args in calls if "diff" in args and "--name-status" not in args]
    assert diff_specs == ["a...b", "a..b"]
    assert any("a..b" in args and "--name-status" in args for args in calls)


def test_model_flows_become_goal_shards():
    flow = ProposedFlow(
        name="Toggle dark mode",
        goal="Turn on dark mode from Settings",
        priority=1,
        steps=['Tap "Settings"', "Flip the 'Dark Mode' toggle"],
        expected=['The "Dark Mode" switch is on'],
        touched_files=["Sources/SettingsView.swift"],
    )
    provider, plan = _plan_for([flow])
    assert provider.calls == ["propose_flows"]
    assert plan.source == "model"
    assert plan.usage == Usage(provider="fake", model="fake")
    [shard] = plan.shards
    assert shard.id == ""
    assert shard.kind == "exploratory"
    assert shard.name == "Toggle dark mode"
    assert shard.tags == [FLOW_TAG, "priority:1", "flow:toggle-dark-mode"]
    assert shard.goal == (
        "Turn on dark mode from Settings. Steps: Tap Settings; Flip the Dark Mode toggle. "
        'Expect: The "Dark Mode" switch is on.'
    )
    _, expected = parse_goal(shard.goal)
    assert expected == ["Dark Mode"]


def test_model_flows_pass_diff_files_and_cap():
    seen = {}

    class Recorder(FakeModelProvider):
        def propose_flows(self, diff, files, *, max_flows=10, timeout_s=120.0):
            seen.update(diff=diff, files=files, max_flows=max_flows)
            return FlowProposal()

    files = [ChangedFile("A.swift", "struct A {}")]
    change = Change(base="a", head="b", diff="+x\n", files=files)
    plan = flows_from_diff(sample_config(), change, provider=Recorder(), max_flows=4)
    assert seen == {"diff": "+x\n", "files": files, "max_flows": 4}
    assert plan.source == "model"
    assert plan.shards == []
    assert plan.notes == ["model proposed no flows"]


def test_priority_ordering_dedupe_and_cap():
    flows = [
        ProposedFlow(name="Low", goal="Check the footer", priority=5),
        ProposedFlow(name="First high", goal="Open checkout", priority=1),
        ProposedFlow(name="Mid", goal="Edit the profile", priority=3),
        ProposedFlow(name="first  HIGH!", goal="Something else", priority=2),
        ProposedFlow(name="Other", goal="open Checkout.", priority=2),
        ProposedFlow(name="Second high", goal="Pay with card", priority=1),
        ProposedFlow(name="Odd", goal="Weird priority", priority="x"),
        ProposedFlow(name="Way out", goal="Clamped", priority=0),
        ProposedFlow(name="", goal="", priority=1),
    ]
    _, plan = _plan_for(flows)
    assert [s.name for s in plan.shards] == ["First high", "Second high", "Way out", "Mid", "Odd", "Low"]
    assert plan.shards[2].tags[1] == "priority:1"
    assert plan.shards[4].tags[1] == "priority:3"

    class Uncapped(FakeModelProvider):
        def propose_flows(self, diff, files, *, max_flows=10, timeout_s=120.0):
            return FlowProposal(flows=list(flows))

    change = Change("a", "b", diff="+x\n")
    plan = flows_from_diff(sample_config(), change, provider=Uncapped(), max_flows=2)
    assert [s.name for s in plan.shards] == ["First high", "Second high"]

    config = sample_config()
    config.flows.settings = {"max_flows": 3}
    provider = FakeModelProvider(proposals=[FlowProposal(flows=list(flows))])
    plan = flows_from_diff(config, Change("a", "b", diff="+x\n"), provider=provider)
    assert len(plan.shards) == 3


SWIFT_DIFF = """diff --git a/Sources/Login.swift b/Sources/Login.swift
--- a/Sources/Login.swift
+++ b/Sources/Login.swift
@@ -1,4 +1,6 @@
 struct LoginView: View {
     var body: some View {
+        Button("Sign In") { submit() }
+            .accessibilityIdentifier("login.submit")
         Text("Welcome")
     }
 }
diff --git a/Sources/Model.swift b/Sources/Model.swift
--- a/Sources/Model.swift
+++ b/Sources/Model.swift
@@ -1,2 +1,2 @@
 struct Model {
-    var a = 1
+    var a = 2
 }
diff --git a/Sources/Gone.swift b/Sources/Gone.swift
deleted file mode 100644
--- a/Sources/Gone.swift
+++ /dev/null
@@ -1,3 +0,0 @@
-struct GoneView: View {
-    var body: some View { Button("Bye") {} }
-}
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 hi
+Button("NotSwift")
"""

LOGIN = """struct LoginView: View {
    var body: some View {
        Button("Sign In") { submit() }
            .accessibilityIdentifier("login.submit")
        Text("Welcome")
    }
}
"""


def _swift_change() -> Change:
    files = [
        ChangedFile("Sources/Login.swift", LOGIN),
        ChangedFile("Sources/Model.swift", "struct Model {\n    var a = 2\n}\n"),
        ChangedFile("Sources/Gone.swift", ""),
    ]
    return Change(base="a", head="b", diff=SWIFT_DIFF, files=files)


def test_model_error_falls_back_to_heuristic():
    provider = FakeModelProvider(error=ModelTimeout("too slow"))
    plan = flows_from_diff(sample_config(), _swift_change(), provider=provider)
    assert plan.source == "heuristic"
    assert plan.usage is None
    assert plan.notes == ["model failed (ModelTimeout: too slow); used heuristics"]
    [shard] = plan.shards
    assert shard.name == "Login view"
    assert shard.goal == 'Open Login and use "Sign In" and "login.submit".'
    assert shard.tags == [FLOW_TAG, "priority:2", "flow:login-view"]
    assert shard.kind == "exploratory" and shard.id == ""


def test_no_provider_uses_heuristic_with_note():
    plan = flows_from_diff(sample_config(), _swift_change())
    assert plan.source == "heuristic"
    assert plan.notes == ["no model provider; used heuristics"]
    assert [s.name for s in plan.shards] == ["Login view"]


def test_heuristic_without_head_content_and_touched_view_without_controls():
    diff = """diff --git a/Sources/Home.swift b/Sources/Home.swift
--- a/Sources/Home.swift
+++ b/Sources/Home.swift
@@ -10,3 +10,4 @@
 struct HomeView: View {
-    let title = "Old"
+    let title = "New"
+    // tweak
 }
diff --git a/Sources/Panel.swift b/Sources/Panel.swift
--- a/Sources/Panel.swift
+++ b/Sources/Panel.swift
@@ -1,2 +1,3 @@
 var x = 1
+Button("Go") {}
"""
    flows = heuristic_flows(Change("a", "b", diff=diff))
    assert [(f.name, f.goal, f.priority) for f in flows] == [
        ("Home view", "Open Home", 3),
        ("Panel view", 'Open Panel and use "Go"', 2),
    ]
    plan = flows_from_diff(sample_config(), Change("a", "b", diff=diff))
    assert [s.name for s in plan.shards] == ["Panel view", "Home view"]
    assert plan.shards[1].goal == "Open Home."


def test_heuristic_with_no_views_notes_it():
    diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hi\n+there\n"
    plan = flows_from_diff(sample_config(), Change("a", "b", diff=diff))
    assert plan.source == "heuristic"
    assert plan.shards == []
    assert plan.notes == ["no model provider; used heuristics", "no SwiftUI views in the change"]


def test_empty_diff_gives_no_plan():
    provider = FakeModelProvider()
    plan = flows_from_diff(sample_config(), Change("a", "b", diff="  \n"), provider=provider)
    assert plan.source == "none"
    assert plan.shards == []
    assert provider.calls == []


def test_from_diff_off_gives_no_plan():
    plan = flows_from_diff(None, _swift_change(), settings=FlowsSettings(from_diff=False))
    assert plan.source == "none"
    assert plan.notes == ["flows.from_diff is off"]


def test_truncated_change_is_noted():
    change = _swift_change()
    change.truncated = True
    plan = flows_from_diff(sample_config(), change)
    assert plan.notes[0] == f"diff cut to {len(change.diff)} characters"


def test_goal_text_quote_rules():
    assert goal_text("Save a note", ['Tap "New"', 'Tap "Save"'], ["Note appears"]) == (
        'Save a note. Steps: Tap New; Tap "Save". Expect: Note appears.'
    )
    assert goal_text("Open the user's profile", [], []) == "Open the user’s profile."
    assert goal_text("Tap “Done”", ["Tap 'Edit'"], []) == 'Tap "Done". Steps: Tap Edit.'
    assert goal_text("Broken \"quote", [], []) == "Broken quote."
