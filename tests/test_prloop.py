"""C7 — human draft PRs and the autonomous retest loop."""

from __future__ import annotations

import subprocess
from pathlib import Path

from swarmqa.models import CampaignResult, Finding, FixProposal, WorkerResult
from swarmqa.prloop.loop import run_fix_loop
from swarmqa.testing import sample_config


def test_human_writes_one_draft_pr(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    original = (repo / "README.md").read_text(encoding="utf-8")
    head = _rev(repo, "HEAD")
    calls: list[list[str]] = []
    result = _campaign(
        report,
        [
            _worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1", video="media/w1.mp4", shot="media/w1.png")]),
            _worker(
                "w2",
                "s-save",
                "save",
                "failed",
                "scripted",
                [_finding("f2", video="media/w2.mp4", shot="media/w2.png", replay="findings/f2.replay.json")],
            ),
            _worker("w3", "s-open", "open", "passed", "scripted", []),
        ],
    )

    def fixer(*_args, **_kwargs):
        raise AssertionError("human mode must not apply a fix")

    loop = run_fix_loop(
        result,
        sample_config(),
        repo=repo,
        fixer=fixer,
        retest=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("human mode must not retest")),
        gh_runner=_gh(calls),
    )

    assert loop.mode == "human"
    assert loop.stop_reason == "human"
    assert loop.iterations == 0
    assert loop.pr_updates == 1
    assert loop.branch == "aqa/camp-7"
    assert loop.pr_url == "https://example.com/pull/7"
    assert loop.remaining_finding_ids == ["f1"]
    assert _branch(repo) == "aqa/camp-7"
    assert _rev(repo, "main") == head
    assert (repo / "README.md").read_text(encoding="utf-8") == original
    assert _rev(repo, "HEAD") == head

    draft = Path(loop.draft_path)
    assert draft == report / "pr" / "draft.md"
    text = draft.read_text(encoding="utf-8")
    assert "## Title" in text
    assert "Save did nothing" in text
    assert "## Failing shards" in text
    assert "s-save" in text
    assert "s-open" not in text
    assert "click Save" in text
    assert "media/w1.mp4" in text
    assert "media/w2.mp4" in text
    assert "media/w1.png" in text
    assert "media/w2.png" in text
    assert "findings/f1.replay.json" in text
    assert "### Replay path" in text

    assert len(calls) == 1
    args = calls[0]
    assert args[:3] == ["gh", "pr", "create"]
    assert "--draft" in args
    assert args[args.index("--head") + 1] == "aqa/camp-7"
    assert args[args.index("--title") + 1] == "Save did nothing"
    assert Path(args[args.index("--body-file") + 1]) == draft
    _assert_never_merges(calls)


def test_human_skips_branch_when_repo_is_not_git(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    report = tmp_path / "reports" / "camp-7"
    calls: list[list[str]] = []
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    loop = run_fix_loop(result, sample_config(), repo=repo, gh_runner=_gh(calls))

    assert loop.branch is None
    assert loop.stop_reason == "human"
    assert Path(loop.draft_path).is_file()
    assert "Save did nothing" in Path(loop.draft_path).read_text(encoding="utf-8")
    assert calls and calls[0][1:3] == ["pr", "create"]
    assert not (repo / ".git").exists()


def test_human_skips_pr_when_gh_is_missing(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    def which(name: str):
        if name == "gh":
            return None
        return shutil_which(name)

    monkeypatch.setattr("swarmqa.prloop.loop.shutil.which", which)
    loop = run_fix_loop(result, sample_config(), repo=repo)

    assert loop.pr_url is None
    assert loop.pr_updates == 0
    assert loop.stop_reason == "human"
    assert loop.branch == "aqa/camp-7"
    assert Path(loop.draft_path).is_file()


def test_autonomous_goes_green_within_cap(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    base = _rev(repo, "HEAD")
    calls: list[list[str]] = []
    seen: dict[str, list] = {"shards": [], "iterations": []}
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.max_iterations = 3
    config.pr.max_pr_updates = 5
    config.pr.max_wall_time_s = 3600
    failed = _worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])
    other = _worker("w2", "s-open", "open", "failed", "scripted", [_finding("f9", title="Open failed")])
    passed_worker = _worker("w3", "s-ok", "ok", "passed", "scripted", [])
    result = _campaign(report, [failed, other, passed_worker])

    def fixer(findings, repo_path, iteration):
        seen["iterations"].append(iteration)
        assert {item.id for item in findings} == {"f1", "f9"}
        (repo_path / "app.txt").write_text("fixed\n", encoding="utf-8")
        return FixProposal(
            branch="aqa/camp-7",
            title="Fix save and open",
            body="Handle both clicks.",
            commit_message="aqa: fix save",
            changed_files=["app.txt"],
        )

    def retest(failed_shards):
        seen["shards"].append(list(failed_shards))
        return _campaign(
            report,
            [
                _worker("w1", "s-save", "save", "passed", "scripted", []),
                _worker("w2", "s-open", "open", "passed", "scripted", []),
                passed_worker,
            ],
        )

    loop = run_fix_loop(result, config, repo=repo, fixer=fixer, retest=retest, gh_runner=_gh(calls))

    assert loop.stop_reason == "green"
    assert loop.iterations == 1
    assert loop.pr_updates == 1
    assert loop.remaining_finding_ids == []
    assert loop.pr_url == "https://example.com/pull/7"
    assert loop.branch == "aqa/camp-7"
    assert seen["iterations"] == [1]
    assert len(seen["shards"]) == 1
    assert {shard.shard_id for shard in seen["shards"][0]} == {"s-save", "s-open"}
    assert all(shard.status == "failed" for shard in seen["shards"][0])
    assert (repo / "app.txt").read_text(encoding="utf-8") == "fixed\n"
    assert _rev(repo, "main") == base
    assert _rev(repo, "HEAD") != base
    assert _branch(repo) == "aqa/camp-7"
    assert _subject(repo) == "aqa: fix save"
    assert calls[0][:3] == ["gh", "pr", "create"]
    assert "--draft" in calls[0]
    assert calls[0][calls[0].index("--title") + 1] == "Fix save and open"
    _assert_never_merges(calls)
    text = Path(loop.draft_path).read_text(encoding="utf-8")
    assert "Scripted retest: green" in text
    assert "Save did nothing" in text


def test_autonomous_stops_at_max_iterations(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    calls: list[list[str]] = []
    fixer_calls = {"n": 0}
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.max_iterations = 2
    config.pr.max_pr_updates = 10
    config.pr.max_wall_time_s = 3600
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    def fixer(_findings, _repo, _iteration):
        fixer_calls["n"] += 1
        return _proposal()

    def retest(_failed):
        return result

    loop = run_fix_loop(result, config, repo=repo, fixer=fixer, retest=retest, gh_runner=_gh(calls))

    assert loop.stop_reason == "max_iterations"
    assert loop.iterations == 2
    assert loop.pr_updates == 2
    assert fixer_calls["n"] == 2
    assert loop.remaining_finding_ids == ["f1"]
    assert loop.pr_url == "https://example.com/pull/7"
    assert calls[0][1:3] == ["pr", "create"]
    assert calls[1][1:3] == ["pr", "edit"]
    _assert_never_merges(calls)
    assert "s-save" in Path(loop.draft_path).read_text(encoding="utf-8")


def test_autonomous_stops_at_max_pr_updates(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    calls: list[list[str]] = []
    fixer_calls = {"n": 0}
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.max_iterations = 5
    config.pr.max_pr_updates = 1
    config.pr.max_wall_time_s = 3600
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    def fixer(_findings, _repo, _iteration):
        fixer_calls["n"] += 1
        return _proposal()

    loop = run_fix_loop(
        result,
        config,
        repo=repo,
        fixer=fixer,
        retest=lambda _failed: result,
        gh_runner=_gh(calls),
    )

    assert loop.stop_reason == "max_pr_updates"
    assert loop.iterations == 1
    assert loop.pr_updates == 1
    assert fixer_calls["n"] == 1
    assert loop.remaining_finding_ids == ["f1"]
    assert len(calls) == 1
    assert calls[0][1:3] == ["pr", "create"]
    assert "--draft" in calls[0]
    _assert_never_merges(calls)


def test_autonomous_stops_at_max_wall_time(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    calls: list[list[str]] = []
    clock = {"now": 0.0}
    fixer_calls = {"n": 0}
    monkeypatch.setattr("swarmqa.prloop.loop.time.monotonic", lambda: clock["now"])
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.max_iterations = 5
    config.pr.max_pr_updates = 5
    config.pr.max_wall_time_s = 30
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    def fixer(_findings, _repo, _iteration):
        fixer_calls["n"] += 1
        clock["now"] = 100.0
        return _proposal()

    loop = run_fix_loop(
        result,
        config,
        repo=repo,
        fixer=fixer,
        retest=lambda _failed: result,
        gh_runner=_gh(calls),
    )

    assert loop.stop_reason == "max_wall_time"
    assert loop.iterations == 1
    assert fixer_calls["n"] == 1
    assert loop.pr_updates == 1
    assert loop.remaining_finding_ids == ["f1"]
    assert loop.pr_url == "https://example.com/pull/7"
    assert calls[0][1:3] == ["pr", "create"]
    _assert_never_merges(calls)


def test_autonomous_wall_time_already_spent_leaves_pr_open(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    calls: list[list[str]] = []
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.max_iterations = 3
    config.pr.max_pr_updates = 5
    config.pr.max_wall_time_s = 0
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    def fixer(*_args, **_kwargs):
        raise AssertionError("wall-time cap must stop before a fix")

    loop = run_fix_loop(
        result,
        config,
        repo=repo,
        fixer=fixer,
        retest=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not retest")),
        gh_runner=_gh(calls),
    )

    assert loop.stop_reason == "max_wall_time"
    assert loop.iterations == 0
    assert loop.remaining_finding_ids == ["f1"]
    assert loop.pr_url == "https://example.com/pull/7"
    assert len(calls) == 1
    assert "--draft" in calls[0]
    _assert_never_merges(calls)


def test_autonomous_no_fixer_does_not_spin(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AQA_FIX_COMMAND", raising=False)
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    calls: list[list[str]] = []
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.fix_command = None
    config.pr.max_iterations = 10
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    loop = run_fix_loop(result, config, repo=repo, gh_runner=_gh(calls))

    assert loop.stop_reason == "no_fixer"
    assert loop.iterations == 0
    assert loop.pr_updates == 0
    assert loop.remaining_finding_ids == ["f1"]
    assert calls == []
    text = Path(loop.draft_path).read_text(encoding="utf-8")
    assert "Save did nothing" in text
    assert "click Save" in text


def test_autonomous_fix_command_then_green(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AQA_FIX_COMMAND", "python3 -c 'open(\"ignored.txt\",\"w\").write(\"no\")'")
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.fix_command = (
        "python3 -c \"from pathlib import Path; Path('fixed.txt').write_text('ok\\n', encoding='utf-8')\""
    )
    result = _campaign(report, [_worker("w1", "s-save", "save", "failed", "scripted", [_finding("f1")])])

    def retest(failed_shards):
        assert [shard.shard_id for shard in failed_shards] == ["s-save"]
        return _campaign(report, [_worker("w1", "s-save", "save", "passed", "scripted", [])])

    loop = run_fix_loop(result, config, repo=repo, retest=retest, gh_runner=_gh([]))

    assert loop.stop_reason == "green"
    assert loop.iterations == 1
    assert (repo / "fixed.txt").read_text(encoding="utf-8") == "ok\n"
    assert not (repo / "ignored.txt").exists()
    assert _subject(repo) == "aqa: autonomous fix iteration 1"


def test_autonomous_env_fix_command(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    report = tmp_path / "reports" / "camp-7"
    monkeypatch.setenv(
        "AQA_FIX_COMMAND",
        "python3 -c \"from pathlib import Path; Path('from-env.txt').write_text('ok\\n', encoding='utf-8')\"",
    )
    config = sample_config()
    config.pr.mode = "autonomous"
    config.pr.fix_command = None
    result = _campaign(report, [_worker("w1", "s-save", "save", "error", "scripted", [_finding("f1")])])

    loop = run_fix_loop(
        result,
        config,
        repo=repo,
        retest=lambda _failed: _campaign(report, [_worker("w1", "s-save", "save", "passed", "scripted", [])]),
        gh_runner=_gh([]),
    )

    assert loop.stop_reason == "green"
    assert (repo / "from-env.txt").read_text(encoding="utf-8") == "ok\n"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("app\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


def _finding(
    finding_id: str,
    *,
    title: str = "Save did nothing",
    video: str = "media/fail.mp4",
    shot: str = "media/fail.png",
    replay: str = "findings/f1.replay.json",
) -> Finding:
    return Finding(
        id=finding_id,
        title=title,
        severity="high",
        kind="assertion",
        steps=["click Save"],
        fingerprint="abc123" if title == "Save did nothing" else finding_id,
        worker_id="w",
        backend="local",
        shard_id="s-save",
        screenshots=[shot],
        video=video,
        replay_json=replay,
    )


def _worker(
    worker_id: str,
    shard_id: str,
    name: str,
    status: str,
    kind: str,
    findings: list[Finding],
) -> WorkerResult:
    for finding in findings:
        finding.worker_id = worker_id
        if worker_id not in finding.worker_ids:
            finding.worker_ids.insert(0, worker_id)
        finding.shard_id = shard_id
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard_id,
        shard_name=name,
        shard_kind=kind,
        status=status,
        findings=findings,
        backend="local",
    )


def _campaign(report: Path, workers: list[WorkerResult]) -> CampaignResult:
    return CampaignResult(
        campaign_id="camp-7",
        report_dir=str(report),
        results=workers,
        backend="local",
        exit_code=1,
    )


def _proposal() -> FixProposal:
    return FixProposal(
        branch="aqa/camp-7",
        title="Fix save",
        body="Patch the handler.",
        commit_message="aqa: fix save",
        changed_files=[],
    )


def _gh(calls: list[list[str]]):
    def runner(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="https://example.com/pull/7\n",
            stderr="",
        )

    return runner


def _assert_never_merges(calls: list[list[str]]) -> None:
    for args in calls:
        lowered = [part.lower() for part in args]
        assert "merge" not in lowered
        assert "close" not in lowered


def _rev(repo: Path, ref: str) -> str:
    return subprocess.check_output(["git", "rev-parse", ref], cwd=repo, text=True).strip()


def _branch(repo: Path) -> str:
    return subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()


def _subject(repo: Path) -> str:
    return subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=repo, text=True).strip()


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)
