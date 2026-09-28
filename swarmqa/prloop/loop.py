"""C7 — one campaign-level fix loop.

Workers must not call this. Human mode writes a single draft pull request and
returns. Autonomous mode asks a fixer to edit the campaign branch, updates that
same PR, and retests only the shards that failed until scripted shards are
green or a safety cap trips. This module never merges to the default branch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from swarmqa.models import CampaignConfig, CampaignResult, Finding, FixLoopResult, FixProposal, WorkerResult
from swarmqa.report.dedup import dedup_findings
from swarmqa.serialize import to_plain

_FAILED_STATUSES = {"failed", "error"}
_NON_SCRIPTED_KINDS = {"exploratory", "suite", "visual"}
_URL = re.compile(r"https?://\S+")


def run_fix_loop(
    result: CampaignResult,
    config: CampaignConfig,
    *,
    repo: Path,
    fixer: Callable[..., FixProposal] | None = None,
    retest: Callable[..., CampaignResult] | None = None,
    gh_runner: Callable | None = None,
) -> FixLoopResult:
    """Run the one fix/PR loop for a campaign.

    `fixer(findings, repo, iteration)` is 1-based. When it is omitted, autonomous
    mode runs `config.pr.fix_command` or the `AQA_FIX_COMMAND` environment
    variable. `retest` receives the failed worker results (status `failed` or
    `error`), not the full queue. `gh_runner(args)` is a `subprocess.run` seam:
    `args` starts with `gh`.
    """
    repo_path = repo if isinstance(repo, Path) else Path(repo)
    branch_name = f"aqa/{result.campaign_id}"
    branch = _ensure_branch(repo_path, branch_name)
    if config.pr.mode == "autonomous":
        return _run_autonomous(
            result,
            config,
            repo=repo_path,
            branch_name=branch_name,
            branch=branch,
            fixer=fixer,
            retest=retest,
            gh_runner=gh_runner,
        )
    return _run_human(
        result,
        repo=repo_path,
        branch_name=branch_name,
        branch=branch,
        gh_runner=gh_runner,
    )


def _run_human(
    result: CampaignResult,
    *,
    repo: Path,
    branch_name: str,
    branch: str | None,
    gh_runner: Callable | None,
) -> FixLoopResult:
    """Write one draft and return. Do not run a fixer or commit code changes."""
    findings = _merged_findings(result)
    draft = _write_draft(result, findings, branch_name=branch_name, status="human")
    pr_url, pr_updates = _publish(
        repo,
        gh_runner,
        branch_name=branch_name,
        title=_title_for(result.campaign_id, findings),
        draft=draft,
        pr_url=None,
        opened=False,
    )
    return FixLoopResult(
        mode="human",
        iterations=0,
        pr_updates=pr_updates,
        branch=branch,
        pr_url=pr_url,
        stop_reason="human",
        remaining_finding_ids=[finding.id for finding in findings],
        draft_path=str(draft),
    )


def _run_autonomous(
    result: CampaignResult,
    config: CampaignConfig,
    *,
    repo: Path,
    branch_name: str,
    branch: str | None,
    fixer: Callable[..., FixProposal] | None,
    retest: Callable[..., CampaignResult] | None,
    gh_runner: Callable | None,
) -> FixLoopResult:
    active = fixer or _command_fixer(config, branch_name)
    findings = _merged_findings(result)
    if active is None:
        draft = _write_draft(
            result,
            findings,
            branch_name=branch_name,
            status="no_fixer",
            report_dir=result.report_dir,
        )
        return FixLoopResult(
            mode="autonomous",
            iterations=0,
            pr_updates=0,
            branch=branch,
            pr_url=None,
            stop_reason="no_fixer",
            remaining_finding_ids=[finding.id for finding in findings],
            draft_path=str(draft),
        )

    started = time.monotonic()
    current = result
    iterations = 0
    pr_updates = 0
    pr_url: str | None = None
    opened = False
    stop_reason: str | None = None
    remembered: list[Finding] = list(findings)
    report_dir = Path(result.report_dir)

    while _work_remaining(current):
        reason = _cap_reason(
            config,
            iterations=iterations,
            pr_updates=pr_updates,
            elapsed=time.monotonic() - started,
        )
        if reason:
            stop_reason = reason
            break

        iterations += 1
        latest = _merged_findings(current)
        if latest:
            remembered = latest
        failed = _failed_shards(current)
        proposal = active(list(latest), repo, iterations)
        if not isinstance(proposal, FixProposal):
            proposal = FixProposal(
                branch=branch_name,
                title=_title_for(result.campaign_id, latest),
                body="",
                commit_message=f"aqa: autonomous fix iteration {iterations}",
            )
        _commit_proposal(repo, proposal, report_dir)
        title = _one_line(proposal.title) if proposal.title.strip() else _title_for(result.campaign_id, latest)
        draft = _write_draft(
            current,
            latest,
            branch_name=branch_name,
            proposal_body=proposal.body,
            campaign_id=result.campaign_id,
            report_dir=result.report_dir,
        )
        pr_url, added, opened = _publish_counting(
            repo,
            gh_runner,
            branch_name=branch_name,
            title=title,
            draft=draft,
            pr_url=pr_url,
            opened=opened,
        )
        pr_updates += added

        if retest is None:
            # Nothing can clear the failures. Stop with the iteration cap
            # instead of calling the fixer again.
            stop_reason = "max_iterations"
            break

        nxt = retest(list(failed))
        if not isinstance(nxt, CampaignResult):
            stop_reason = "max_iterations"
            break
        current = nxt
        if not _blocks_green(current):
            stop_reason = "green"
            break
    else:
        stop_reason = "green"

    if stop_reason is None:
        stop_reason = "green"

    visible = _merged_findings(current) or remembered
    draft = _write_draft(
        current,
        visible,
        branch_name=branch_name,
        status=stop_reason,
        campaign_id=result.campaign_id,
        report_dir=result.report_dir,
        green=stop_reason == "green",
    )
    if stop_reason != "green" and not opened and pr_updates < config.pr.max_pr_updates:
        title = _title_for(result.campaign_id, visible)
        pr_url, added, opened = _publish_counting(
            repo,
            gh_runner,
            branch_name=branch_name,
            title=title,
            draft=draft,
            pr_url=pr_url,
            opened=opened,
        )
        pr_updates += added

    remaining: list[str] = []
    if stop_reason != "green":
        remaining = [finding.id for finding in _merged_findings(current)]
        if not remaining:
            remaining = [finding.id for finding in visible]

    return FixLoopResult(
        mode="autonomous",
        iterations=iterations,
        pr_updates=pr_updates,
        branch=branch,
        pr_url=pr_url,
        stop_reason=stop_reason,
        remaining_finding_ids=remaining,
        draft_path=str(draft),
    )


def _command_fixer(config: CampaignConfig, branch_name: str) -> Callable[..., FixProposal] | None:
    command = (config.pr.fix_command or "").strip()
    if not command:
        command = (os.environ.get("AQA_FIX_COMMAND") or "").strip()
    if not command:
        return None

    def _run(findings: list[Finding], repo: Path, iteration: int) -> FixProposal:
        env = os.environ.copy()
        env["AQA_ITERATION"] = str(iteration)
        env["AQA_FINDING_IDS"] = ",".join(finding.id for finding in findings)
        env["AQA_FINDINGS_JSON"] = json.dumps(to_plain(findings))
        subprocess.run(
            command,
            shell=True,
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        titles = ", ".join(finding.title for finding in findings if finding.title.strip())
        return FixProposal(
            branch=branch_name,
            title=titles or f"AQA fix iteration {iteration}",
            body="",
            commit_message=f"aqa: autonomous fix iteration {iteration}",
            changed_files=_dirty_paths(repo, None),
        )

    return _run


def _cap_reason(
    config: CampaignConfig,
    *,
    iterations: int,
    pr_updates: int,
    elapsed: float,
) -> str | None:
    """Return the first exhausted cap, in contract order."""
    if iterations >= config.pr.max_iterations:
        return "max_iterations"
    if pr_updates >= config.pr.max_pr_updates:
        return "max_pr_updates"
    if elapsed >= config.pr.max_wall_time_s:
        return "max_wall_time"
    return None


def _work_remaining(result: CampaignResult) -> bool:
    return bool(_merged_findings(result) or _blocks_green(result))


def _merged_findings(result: CampaignResult) -> list[Finding]:
    raw = [finding for worker in result.results for finding in worker.findings]
    return dedup_findings(raw)


def _failed_shards(result: CampaignResult) -> list[WorkerResult]:
    return [worker for worker in result.results if worker.status in _FAILED_STATUSES]


def _blocks_green(result: CampaignResult) -> list[WorkerResult]:
    """Failed shards that still count as scripted.

    Exploratory, suite, and visual shards do not block `stop_reason="green"`.
    A failed shard that did not declare a kind is treated as scripted so an
    unlabelled failure cannot exit early.
    """
    blocking: list[WorkerResult] = []
    for worker in _failed_shards(result):
        if worker.shard_kind in _NON_SCRIPTED_KINDS:
            continue
        blocking.append(worker)
    return blocking


def _ensure_branch(repo: Path, branch: str) -> str | None:
    """Create or switch to `branch` when `repo` is its own git checkout."""
    if shutil.which("git") is None or not repo.is_dir():
        return None
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if toplevel.returncode != 0:
        return None
    if Path(toplevel.stdout.strip()).resolve() != repo.resolve():
        return None
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if current.stdout.strip() == branch:
        return branch
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    command = ["git", "checkout", branch] if exists.returncode == 0 else ["git", "checkout", "-b", branch]
    created = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    if created.returncode != 0:
        return None
    return branch


def _commit_proposal(repo: Path, proposal: FixProposal, report_dir: Path) -> None:
    """Commit fixer edits on the campaign branch. Never merge or push."""
    if shutil.which("git") is None or not (repo / ".git").exists():
        return
    paths = _proposal_paths(repo, proposal, report_dir)
    if not paths:
        paths = _dirty_paths(repo, report_dir)
    if not paths:
        return
    added = subprocess.run(
        ["git", "add", "--all", "--", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode != 0:
        return
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode == 0:
        return
    message = proposal.commit_message.strip() or "aqa: apply fix"
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Autonomous QA",
            "-c",
            "user.email=aqa@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _proposal_paths(repo: Path, proposal: FixProposal, report_dir: Path) -> list[str]:
    paths: list[str] = []
    for raw in proposal.changed_files:
        path = Path(raw)
        if path.is_absolute():
            try:
                relative = path.resolve().relative_to(repo.resolve())
            except ValueError:
                continue
        else:
            relative = Path(raw)
        if _is_report_path(repo, relative, report_dir):
            continue
        text = relative.as_posix()
        if text and text not in paths:
            paths.append(text)
    return paths


def _dirty_paths(repo: Path, report_dir: Path | None) -> list[str]:
    if shutil.which("git") is None or not (repo / ".git").exists():
        return []
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return []
    paths: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        relative = Path(raw)
        if report_dir is not None and _is_report_path(repo, relative, report_dir):
            continue
        text = relative.as_posix()
        if text and text not in paths:
            paths.append(text)
    return paths


def _is_report_path(repo: Path, relative: Path, report_dir: Path) -> bool:
    candidate = (repo / relative).resolve()
    root = report_dir.resolve()
    return candidate == root or root in candidate.parents


def _publish(
    repo: Path,
    gh_runner: Callable | None,
    *,
    branch_name: str,
    title: str,
    draft: Path,
    pr_url: str | None,
    opened: bool,
) -> tuple[str | None, int]:
    pr_url, added, _opened = _publish_counting(
        repo,
        gh_runner,
        branch_name=branch_name,
        title=title,
        draft=draft,
        pr_url=pr_url,
        opened=opened,
    )
    return pr_url, added


def _publish_counting(
    repo: Path,
    gh_runner: Callable | None,
    *,
    branch_name: str,
    title: str,
    draft: Path,
    pr_url: str | None,
    opened: bool,
) -> tuple[str | None, int, bool]:
    """Create or edit the single campaign PR. Returns url, update count, opened."""
    if gh_runner is None and shutil.which("gh") is None:
        return pr_url, 0, opened
    if opened or pr_url:
        target = pr_url or branch_name
        args = ["gh", "pr", "edit", target, "--title", title, "--body-file", str(draft)]
    else:
        args = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--title",
            title,
            "--body-file",
            str(draft),
            "--head",
            branch_name,
        ]
    url, ok = _invoke_gh(repo, gh_runner, args)
    if ok:
        opened = True
        if url:
            pr_url = url
    return pr_url, 1, opened


def _invoke_gh(repo: Path, gh_runner: Callable | None, args: list[str]) -> tuple[str | None, bool]:
    try:
        if gh_runner is not None:
            proc = gh_runner(list(args))
        else:
            proc = subprocess.run(
                args,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    return _url_from(proc), _succeeded(proc)


def _succeeded(proc: object) -> bool:
    if proc is None:
        return False
    return getattr(proc, "returncode", 1) == 0


def _url_from(proc: object) -> str | None:
    if not _succeeded(proc):
        return None
    stdout = getattr(proc, "stdout", "") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    match = _URL.search(stdout)
    if not match:
        return None
    return match.group(0).rstrip(").,")


def _write_draft(
    result: CampaignResult,
    findings: list[Finding],
    *,
    branch_name: str,
    status: str = "",
    proposal_body: str = "",
    campaign_id: str | None = None,
    report_dir: str | None = None,
    green: bool = False,
) -> Path:
    campaign = campaign_id or result.campaign_id
    directory = report_dir if report_dir is not None else result.report_dir
    draft = Path(directory) / "pr" / "draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(
        _render_draft(
            campaign,
            result,
            findings,
            branch_name=branch_name,
            status=status,
            proposal_body=proposal_body,
            green=green,
        ),
        encoding="utf-8",
    )
    return draft


def _render_draft(
    campaign_id: str,
    result: CampaignResult,
    findings: list[Finding],
    *,
    branch_name: str,
    status: str,
    proposal_body: str,
    green: bool,
) -> str:
    title = _title_for(campaign_id, findings)
    lines = [
        f"# {title}",
        "",
        "## Title",
        "",
        title,
        "",
        f"Campaign: `{campaign_id}`",
        f"Branch: `{branch_name}`",
    ]
    if status:
        lines.append(f"Status: `{status}`")
    if green:
        lines.append("Scripted retest: green")
    lines.extend(["", "## Failing shards", ""])
    lines.extend(_shard_lines(result))
    if proposal_body.strip():
        lines.extend(["", "## Proposed fix", "", proposal_body.strip()])
    lines.extend(["", "## Findings", ""])
    if not findings:
        lines.append("No findings.")
    for finding in findings:
        lines.extend(_finding_lines(finding))
    lines.append("")
    return "\n".join(lines)


def _shard_lines(result: CampaignResult) -> list[str]:
    failed = _failed_shards(result)
    if not failed:
        return ["None."]
    lines: list[str] = []
    for shard in failed:
        name = shard.shard_name or shard.shard_id or "shard"
        kind = shard.shard_kind or "scripted"
        lines.append(f"- {name} (`{shard.shard_id}`) status `{shard.status}` kind `{kind}`")
    return lines


def _finding_lines(finding: Finding) -> list[str]:
    lines = [
        f"### {finding.title or finding.id}",
        "",
        f"- id: `{finding.id}`",
        f"- severity: {finding.severity}",
        f"- kind: {finding.kind}",
        "",
        "### Steps",
        "",
    ]
    if finding.steps:
        lines.extend(f"{index}. {step}" for index, step in enumerate(finding.steps, start=1))
    else:
        lines.append("None.")
    videos = _videos(finding)
    lines.extend(["", "### Video", ""])
    if videos:
        lines.extend(f"- {video}" for video in videos)
    else:
        lines.append("None.")
    lines.extend(["", "### Screenshots", ""])
    if finding.screenshots:
        lines.extend(f"- {shot}" for shot in finding.screenshots)
    else:
        lines.append("None.")
    lines.extend(["", "### Replay path", "", finding.replay_json or "None.", ""])
    if finding.details.strip():
        lines.extend(["### Details", "", finding.details.strip(), ""])
    return lines


def _videos(finding: Finding) -> list[str]:
    videos: list[str] = []
    if finding.video:
        videos.append(finding.video)
    extra = finding.environment.get("extra_videos", "")
    for part in extra.split(","):
        video = part.strip()
        if video and video not in videos:
            videos.append(video)
    return videos


def _title_for(campaign_id: str, findings: list[Finding]) -> str:
    if len(findings) == 1 and findings[0].title.strip():
        return _one_line(findings[0].title)
    if findings:
        return _one_line(f"AQA {campaign_id}: {len(findings)} findings")
    return _one_line(f"AQA {campaign_id}")


def _one_line(text: str, limit: int = 240) -> str:
    cleaned = " ".join(text.split())
    return (cleaned[:limit] or "AQA findings").strip()
