"""Wave G: the GitHub trigger (`swarmqa.github`) against a fake `gh`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmqa.config import load_config
from swarmqa.errors import ConfigError
from swarmqa.github import report as report_mod
from swarmqa.github.cli import main as watch_main
from swarmqa.github.client import GitHubClient, GitHubError
from swarmqa.github.events import Event, EventResult, PlatformOutcome
from swarmqa.github.report import MARKER, Reporter, conclusion, headline, render
from swarmqa.github.settings import GitHubConfigError, GitHubSettings
from swarmqa.github.watch import Watcher, WatchState, build_repo, evaluate_sha, poll, run_sha, use_gh_credentials
from swarmqa.models import BuildArtifact, CampaignConfig, CampaignResult, Finding

REPO = "acme/app"
SHA1 = "a" * 40
SHA2 = "b" * 40
MAIN1 = "c" * 40
MAIN2 = "d" * 40


def pull_json(number: int, sha: str, **extra) -> dict:
    return {
        "number": number,
        "head": {"sha": sha, "ref": f"feature-{number}"},
        "base": {"ref": "main"},
        "title": f"PR {number}",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": "open",
        **extra,
    }


class FakeGh:
    """Answers `gh api` calls from `routes` keyed by (method, path); records every call."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, args, **kwargs):
        assert args[:2] == ["gh", "api"]
        method = args[args.index("--method") + 1]
        body = None
        if "--input" in args:
            body = json.loads(Path(args[args.index("--input") + 1]).read_text())
            path = args[args.index("--input") - 1]
        else:
            path = args[-1]
        self.calls.append((method, path, body))
        answer = self.routes.get((method, path))
        if answer is None:
            return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
        if isinstance(answer, Exception):
            return subprocess.CompletedProcess(args, 1, "", str(answer))
        value = answer(body) if callable(answer) else answer
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    def writes(self) -> list[tuple[str, str, object]]:
        return [call for call in self.calls if call[0] != "GET"]


def finding(fid: str = "f1", *, severity: str = "high", advisory: bool = False, **extra) -> Finding:
    return Finding(
        id=fid, title=f"Title {fid}", severity=severity, kind="unresponsive" if not advisory else "visual_judgment",
        steps=["open", "tap"], fingerprint=f"fp-{fid}", worker_id="w1", backend="local",
        advisory=advisory, confidence=0.6 if advisory else 1.0, **extra,
    )


def settings(**extra) -> GitHubSettings:
    base = {"repo": REPO, "platforms": ["ios"]}
    base.update(extra)
    return GitHubSettings.from_mapping(base)


# Settings -------------------------------------------------------------------


def test_settings_defaults_post_nothing():
    value = GitHubSettings.from_mapping({})
    assert value.report == "none" and value.handoff == "none"
    assert not value.posts_check and not value.posts_comment
    assert value.events == ["pr", "push"]


def test_settings_fall_back_to_issue_repo_and_app_platform():
    config = CampaignConfig()
    config.issues.github_repo = "acme/other"
    config.app.platform = "ios"
    value = GitHubSettings.from_config(config)
    assert value.repo == "acme/other" and value.platforms == ["ios"]


@pytest.mark.parametrize(
    "table, message",
    [
        ({"reprot": "check"}, "github.reprot: unknown key"),
        ({"report": "always"}, "github.report: must be one of"),
        ({"handoff": "gpt"}, "github.handoff: must be one of"),
        ({"platforms": ["android"]}, "github.platforms: unknown android"),
        ({"repo": "noslash"}, "github.repo: must be owner/name"),
        ({"poll_interval_s": 1}, "github.poll_interval_s"),
    ],
)
def test_settings_reject_bad_values(table, message):
    with pytest.raises(GitHubConfigError) as caught:
        GitHubSettings.from_mapping(table)
    assert any(message in error for error in caught.value.errors)


def test_config_validates_github_table(tmp_path):
    path = tmp_path / "aqa.config.toml"
    path.write_text('[github]\nreport = "everything"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert any("github.report" in error for error in caught.value.errors)
    path.write_text('[github]\nreport = "both"\nrepo = "acme/app"\n')
    assert load_config(path).github.settings == {"report": "both", "repo": "acme/app"}


# Client ---------------------------------------------------------------------


def test_client_sends_bodies_as_json_and_reports_http_status():
    gh = FakeGh({("POST", f"repos/{REPO}/check-runs"): {"id": 7}})
    client = GitHubClient(REPO, runner=gh)
    assert client.create_check_run({"name": "x", "output": {"summary": "s"}}) == {"id": 7}
    assert gh.calls[-1] == ("POST", f"repos/{REPO}/check-runs", {"name": "x", "output": {"summary": "s"}})
    with pytest.raises(GitHubError) as caught:
        client.pull(3)
    assert caught.value.status == 404


def test_latest_merged_picks_the_newest_merge():
    gh = FakeGh({("GET", f"repos/{REPO}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=30"): [
        pull_json(1, SHA1, state="closed", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=MAIN1),
        pull_json(2, SHA2, state="closed", merged_at=None),
        pull_json(3, SHA2, state="closed", merged_at="2026-02-01T00:00:00Z", merge_commit_sha=MAIN2),
    ]})
    pull = GitHubClient(REPO, runner=gh).latest_merged("main")
    assert pull.number == 3 and pull.merge_commit_sha == MAIN2


# Polling --------------------------------------------------------------------


def routes(pulls: list[dict], main_head: str) -> dict:
    return {
        ("GET", f"repos/{REPO}/pulls?state=open&sort=updated&direction=desc&per_page=100"): pulls,
        ("GET", f"repos/{REPO}/commits/main"): {"sha": main_head},
    }


def test_first_poll_records_a_baseline_then_reports_changes(tmp_path):
    state = WatchState(tmp_path / "state.json")
    gh = FakeGh(routes([pull_json(1, SHA1)], MAIN1))
    client = GitHubClient(REPO, runner=gh)
    assert poll(client, settings(), state, "main") == []
    assert state.path.is_file() and state.seen(SHA1) and state.branches == {"main": MAIN1}

    gh.routes.update(routes([pull_json(1, SHA2), pull_json(2, SHA1)], MAIN2))
    events = poll(client, settings(), WatchState(state.path), "main")
    assert [(e.kind, e.sha, e.pr) for e in events] == [("pr", SHA2, 1), ("push", MAIN2, None)]


def test_dry_run_poll_leaves_the_state_file_alone(tmp_path):
    state = WatchState(tmp_path / "state.json")
    gh = FakeGh(routes([pull_json(1, SHA1)], MAIN1))
    poll(GitHubClient(REPO, runner=gh), settings(), state, "main", save=False)
    assert not state.path.exists()


def test_events_setting_limits_what_is_watched(tmp_path):
    state = WatchState(tmp_path / "state.json")
    state.save()
    gh = FakeGh(routes([pull_json(1, SHA1)], MAIN1))
    events = poll(GitHubClient(REPO, runner=gh), settings(events=["push"]), state, "main")
    assert events == [] and state.branches == {"main": MAIN1}
    assert all("pulls" not in call[1] for call in gh.calls)


# Processing -----------------------------------------------------------------


def fake_tester(findings: list[Finding] | None = None, *, error: str | None = None):
    calls = []

    def tester(event, config, settings, *, repo=None, log=print):
        calls.append(event)
        outcome = PlatformOutcome("ios", campaign_id="c1", report_dir="/tmp/c1",
                                  findings=list(findings or []), error=error, exit_code=1 if findings else 0)
        return EventResult(event, [outcome])

    tester.calls = calls
    return tester


def make_watcher(tmp_path, gh, value, tester, log=None):
    client = GitHubClient(REPO, runner=gh)
    lines = log if log is not None else []
    reporter = Reporter(client, value, log=lines.append, env={})
    return Watcher(CampaignConfig(), value, client, reporter=reporter,
                   state=WatchState(tmp_path / "s.json"), tester=tester, log=lines.append)


def test_process_skips_a_pr_that_moved_on(tmp_path):
    gh = FakeGh({("GET", f"repos/{REPO}/pulls/1"): pull_json(1, SHA2)})
    tester = fake_tester()
    watcher = make_watcher(tmp_path, gh, settings(), tester)
    result = watcher.process(Event("pr", SHA1, 1))
    assert result.superseded and tester.calls == []
    assert watcher.state.shas[SHA1]["state"] == "superseded"


def test_process_posts_nothing_by_default(tmp_path):
    gh = FakeGh({("GET", f"repos/{REPO}/pulls/1"): pull_json(1, SHA1)})
    watcher = make_watcher(tmp_path, gh, settings(), fake_tester([finding()]))
    result = watcher.process(Event("pr", SHA1, 1))
    assert gh.writes() == [] and result.posted == []
    saved = json.loads((tmp_path / "s.json").read_text())
    assert saved["shas"][SHA1]["state"] == "done" and saved["shas"][SHA1]["campaigns"] == ["c1"]
    assert saved["prs"] == {"1": SHA1}


def test_process_reports_check_and_updates_one_comment(tmp_path):
    gh = FakeGh({
        ("GET", f"repos/{REPO}/pulls/1"): pull_json(1, SHA1),
        ("POST", f"repos/{REPO}/check-runs"): {"id": 11, "html_url": "https://x/check"},
        ("PATCH", f"repos/{REPO}/check-runs/11"): {"id": 11, "html_url": "https://x/check"},
        ("GET", f"repos/{REPO}/issues/1/comments?per_page=100"): [
            {"id": 5, "body": "unrelated"}, {"id": 6, "body": f"{MARKER}\nold"},
        ],
        ("PATCH", f"repos/{REPO}/issues/comments/6"): {"html_url": "https://x/comment"},
    })
    watcher = make_watcher(tmp_path, gh, settings(report="both", handoff="claude"), fake_tester([finding()]))
    result = watcher.process(Event("pr", SHA1, 1))
    writes = gh.writes()
    assert [w[:2] for w in writes] == [
        ("POST", f"repos/{REPO}/check-runs"),
        ("PATCH", f"repos/{REPO}/issues/comments/6"),
        ("PATCH", f"repos/{REPO}/check-runs/11"),
    ]
    assert writes[0][2]["status"] == "in_progress" and writes[0][2]["head_sha"] == SHA1
    assert writes[2][2]["conclusion"] == "failure"
    comment = writes[1][2]["body"]
    assert comment.startswith(MARKER) and "@claude" in comment and "Title f1" in comment
    assert result.posted == ["https://x/comment", "https://x/check"]


def test_check_falls_back_to_commit_status_on_403(tmp_path):
    gh = FakeGh({
        ("POST", f"repos/{REPO}/check-runs"): RuntimeError("must authenticate via a GitHub App (HTTP 403)"),
        ("POST", f"repos/{REPO}/statuses/{MAIN1}"): {},
    })
    log: list[str] = []
    watcher = make_watcher(tmp_path, gh, settings(report="check"), fake_tester(), log=log)
    watcher.process(Event("push", MAIN1, None, "main"))
    statuses = [w for w in gh.writes() if "statuses" in w[1]]
    assert [s[2]["state"] for s in statuses] == ["pending", "success"]
    assert sum("check-runs" in w[1] for w in gh.writes()) == 1  # gave up on check runs after the 403
    assert any("commit status" in line for line in log)
    assert watcher.state.branches == {"main": MAIN1}


def test_superseded_run_reports_its_sha_but_not_the_comment(tmp_path):
    heads = iter([SHA1, SHA2])
    gh = FakeGh({
        ("GET", f"repos/{REPO}/pulls/1"): lambda _: pull_json(1, next(heads)),
        ("POST", f"repos/{REPO}/check-runs"): {"id": 1},
        ("PATCH", f"repos/{REPO}/check-runs/1"): {"id": 1},
    })
    watcher = make_watcher(tmp_path, gh, settings(report="both"), fake_tester([finding()]))
    result = watcher.process(Event("pr", SHA1, 1))
    assert result.superseded
    assert not any("comments" in w[1] for w in gh.writes())
    assert watcher.state.shas[SHA1]["state"] == "superseded"


# Rendering ------------------------------------------------------------------


def test_advisory_findings_do_not_fail_the_check_unless_asked():
    result = EventResult(Event("pr", SHA1, 1), [PlatformOutcome("ios", findings=[finding(advisory=True)])])
    assert conclusion(result, settings()) == "success"
    assert conclusion(result, settings(fail_on_advisory=True)) == "failure"
    assert "@codex" not in render(result, settings(handoff="codex"), for_comment=True)


def test_render_orders_by_severity_and_marks_errors(tmp_path):
    outcome = PlatformOutcome("ios", campaign_id="c9", report_dir=str(tmp_path), findings=[
        finding("low1", severity="low"), finding("crit", severity="critical", replay_json="findings/crit.replay.json"),
    ])
    broken = PlatformOutcome("macos", error="build failed: xcodebuild exited 65\nmore")
    result = EventResult(Event("push", MAIN1, None, "main"), [outcome, broken])
    body = render(result, settings(), run_url="https://ci/run/1")
    assert headline(result, settings()) == "2 finding(s): 1 critical, 1 low"
    assert body.index("Title crit") < body.index("Title low1")
    assert "**macos:** build failed: xcodebuild exited 65" in body
    assert f"aqa replay {tmp_path / 'findings/crit.replay.json'}" in body
    assert "https://ci/run/1" in body and "`c9`" in body and MARKER not in body


def test_evidence_command_uploads_first_screenshot(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"png")
    calls = []

    def runner(args, **kwargs):
        calls.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args, 0, "uploading\nhttps://cdn/shot.png\n", "")

    result = EventResult(Event("pr", SHA1, 1), [PlatformOutcome(
        "ios", report_dir=str(tmp_path), findings=[finding(screenshots=["shot.png"]), finding("f2")])])
    reporter = Reporter(GitHubClient(REPO, runner=FakeGh()), settings(evidence_command="up"), runner=runner, env={})
    assert reporter.upload_evidence(result) == {"f1": "https://cdn/shot.png"}
    assert calls == [{"AQA_FILE": str(tmp_path / "shot.png")}]


def test_actions_run_url():
    env = {"GITHUB_RUN_ID": "9", "GITHUB_REPOSITORY": REPO, "GITHUB_SERVER_URL": "https://ghe"}
    assert report_mod.actions_run_url(env) == f"https://ghe/{REPO}/actions/runs/9"
    assert report_mod.actions_run_url({}) is None


# Building and running -------------------------------------------------------


class FakeBuilder:
    def __init__(self, fail: set[str] = frozenset()):
        self.fail = fail
        self.calls = []

    def build_result(self, sha, platform, *, repo=None, force=False):
        from swarmqa.build.builder import BuildResult
        from swarmqa.build.errors import BuildFailure

        self.calls.append((sha, platform, repo))
        if platform in self.fail:
            return BuildResult(platform, SHA1, failure=BuildFailure(repo or "", SHA1, platform, "build",
                                                                    "xcodebuild exited 65"))
        return BuildResult(platform, SHA1, artifact=BuildArtifact(platform, f"/b/{platform}.app", "dev.app", SHA1))


def test_evaluate_sha_runs_a_campaign_per_platform(tmp_path, monkeypatch):
    config = CampaignConfig()
    config.intents = ["intents"]
    report_dir = tmp_path / "c1"
    report_dir.mkdir()
    from swarmqa.report.findings_json import write_findings_json

    write_findings_json(report_dir, "c1", [finding()])
    seen = []

    def run_campaign(cfg, queue, *, options):
        seen.append((cfg.app.platform, cfg.app.path, cfg.app.bundle_id))
        return CampaignResult("c1", str(report_dir), exit_code=1)

    builder = FakeBuilder(fail={"macos"})
    monkeypatch.setattr("swarmqa.intent.ingest.build_queue", lambda cfg: [SimpleNamespace(id="s1")])
    monkeypatch.setattr("swarmqa.github.watch.use_gh_credentials", lambda env=None: None)
    event = Event("pr", SHA1[:7], 1)
    result = evaluate_sha(event, config, settings(platforms=["ios", "macos"]), repo=REPO,
                          builder=builder, run_campaign=run_campaign, log=lambda _: None)
    assert event.sha == SHA1  # the short SHA is replaced by the resolved one
    assert seen == [("ios", "/b/ios.app", "dev.app")]
    assert config.app.path is None  # the caller's config is untouched
    ios, macos = result.outcomes
    assert [f.id for f in ios.findings] == ["f1"] and ios.campaign_id == "c1"
    assert macos.findings[0].severity == "critical" and macos.error.startswith("build failed")
    assert result.exit_code == 1
    assert builder.calls[0][2] == f"https://github.com/{REPO}.git"


def test_build_repo_prefers_explicit_then_github_then_source_dir():
    config = CampaignConfig()
    config.app.source_dir = "/src/app"
    assert build_repo(config, settings(), REPO) == f"https://github.com/{REPO}.git"
    assert build_repo(config, settings(), None) == "/src/app"
    config.build.settings = {"repo": "git@github.com:acme/app.git"}
    assert build_repo(config, settings(), REPO) == "git@github.com:acme/app.git"
    assert build_repo(config, settings(clone_url="/ci/workspace"), REPO) == "/ci/workspace"


def test_use_gh_credentials_only_when_git_config_env_is_free():
    env: dict[str, str] = {}
    use_gh_credentials(env)
    assert env["GIT_CONFIG_VALUE_0"] == "!gh auth git-credential"
    taken = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x", "GIT_CONFIG_VALUE_0": "y"}
    use_gh_credentials(taken)
    assert taken["GIT_CONFIG_KEY_0"] == "x"


def test_run_sha_finds_the_pr_and_returns_the_exit_code():
    gh = FakeGh({("GET", f"repos/{REPO}/commits/{SHA1}/pulls"): [pull_json(4, SHA1), pull_json(5, SHA2)]})
    tester = fake_tester([finding()])
    config = CampaignConfig()
    config.github.settings = {"repo": REPO}
    code = run_sha(config, SHA1, client=GitHubClient(REPO, runner=gh), tester=tester, log=lambda _: None)
    assert code == 1 and tester.calls[0].pr == 4
    assert gh.writes() == []


# CLI ------------------------------------------------------------------------


def test_watch_dry_run_lists_events_without_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AQA_HOME", str(tmp_path))
    state = tmp_path / "state" / "acme-app.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"shas": {}, "prs": {}, "branches": {"main": MAIN1}}))
    gh = FakeGh(routes([pull_json(1, SHA1)], MAIN2))
    gh.routes[("GET", f"repos/{REPO}")] = {"default_branch": "main"}
    code = watch_main(["--repo", REPO, "--dry-run"], client=GitHubClient(REPO, runner=gh))
    out = capsys.readouterr().out
    assert code == 0
    assert f"would test PR #1 @ {SHA1[:12]}" in out and f"would test main @ {MAIN2[:12]}" in out
    assert json.loads(state.read_text())["branches"] == {"main": MAIN1}


def test_watch_pr_flag_tests_that_pr(tmp_path, monkeypatch):
    monkeypatch.setenv("AQA_HOME", str(tmp_path))
    gh = FakeGh({("GET", f"repos/{REPO}/pulls/3"): pull_json(3, SHA1)})
    tester = fake_tester()
    assert watch_main(["--pr", "3"], client=GitHubClient(REPO, runner=gh), tester=tester) == 0
    assert tester.calls[0].sha == SHA1
    assert (tmp_path / "state" / "acme-app.json").is_file()


def test_run_rejects_github_pr_without_sha(tmp_path):
    from swarmqa.cli import main

    config = tmp_path / "aqa.config.toml"
    config.write_text("")
    assert main(["run", "--config", str(config), "--github-pr", "3"]) == 2
