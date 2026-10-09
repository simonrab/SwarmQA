"""Report an event's findings back to GitHub. Off unless `github.report` is set.

- **check**: a check run named `github.check_name` on the tested SHA, in
  progress while the campaigns run, then completed with a findings table.
  GitHub only lets GitHub Apps create check runs (the Actions `GITHUB_TOKEN`
  works; a personal `gh` login gets 403), so on 403 the reporter falls back
  to a commit status with the same verdict.
- **comment**: one comment per PR, found by a hidden marker and edited in
  place on every run, with the findings, screenshots when an
  `evidence_command` can upload them, and the repro command. With
  `github.handoff` it ends with an `@claude` / `@codex` request to fix them.

The check fails on any non-advisory finding (and on advisory ones too with
`fail_on_advisory`). A superseded run (the PR moved on while it ran) still
reports on its own SHA but leaves the PR comment to the newer run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping

from swarmqa.build.runner import build_runner
from swarmqa.devices.commands import Runner, run
from swarmqa.github.client import GitHubClient, GitHubError
from swarmqa.github.events import Event, EventResult, PlatformOutcome
from swarmqa.github.settings import GitHubSettings
from swarmqa.models import Finding

MARKER = "<!-- swarmqa:report -->"
SUMMARY_LIMIT = 60000
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_HANDOFF = {
    "claude": "@claude",
    "codex": "@codex",
}


def failing(findings: list[Finding], settings: GitHubSettings) -> list[Finding]:
    return [finding for finding in findings if settings.fail_on_advisory or not finding.advisory]


def conclusion(result: EventResult, settings: GitHubSettings) -> str:
    if failing(result.findings, settings):
        return "failure"
    if any(outcome.error for outcome in result.outcomes):
        return "neutral"
    return "success"


def headline(result: EventResult, settings: GitHubSettings) -> str:
    findings = result.findings
    if not findings:
        errors = [outcome for outcome in result.outcomes if outcome.error]
        return f"could not test {len(errors)} platform(s)" if errors else "no findings"
    blocking = failing(findings, settings)
    counts: dict[str, int] = {}
    for finding in blocking:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    parts = [f"{counts[name]} {name}" for name in _SEVERITY_ORDER if name in counts]
    advisory = len(findings) - len(blocking)
    text = f"{len(findings)} finding(s)" + (f": {', '.join(parts)}" if parts else "")
    return text + (f" (+{advisory} advisory)" if advisory and blocking else "")


def repro_command(finding: Finding, outcome: PlatformOutcome, cwd: Path | None = None) -> str | None:
    relative = finding.repro or finding.replay_json
    if not relative or not outcome.report_dir:
        return None
    path = Path(outcome.report_dir) / relative
    try:
        path = path.resolve().relative_to((cwd or Path.cwd()).resolve())
    except ValueError:
        pass
    return f"aqa replay {path}"


def render(
    result: EventResult,
    settings: GitHubSettings,
    *,
    evidence: Mapping[str, str] | None = None,
    run_url: str | None = None,
    for_comment: bool = False,
) -> str:
    """The markdown body shared by the check summary, the comment and stdout."""
    event = result.event
    evidence = evidence or {}
    lines = [MARKER] if for_comment else []
    lines.append(f"## SwarmQA: {headline(result, settings)}")
    lines.append("")
    tested = ", ".join(outcome.platform for outcome in result.outcomes) or "nothing"
    lines.append(f"Commit `{event.sha[:12]}`, tested on {tested}.")
    if result.superseded:
        lines.append("")
        lines.append("_A newer commit was pushed while this ran; see its report._")
    for outcome in result.outcomes:
        if outcome.error:
            lines.append("")
            lines.append(f"**{outcome.platform}:** {outcome.error.splitlines()[0]}")
    rows: list[tuple[Finding, PlatformOutcome]] = [
        (finding, outcome) for outcome in result.outcomes for finding in outcome.findings
    ]
    rows.sort(key=lambda row: (row[0].advisory, _SEVERITY_ORDER.get(row[0].severity, 9), row[0].id))
    shown = rows[: settings.max_findings]
    if shown:
        lines += ["", "| Severity | Platform | Finding | Category |", "| --- | --- | --- | --- |"]
        for finding, outcome in shown:
            advisory = " (advisory)" if finding.advisory else ""
            title = _cell(finding.title)
            lines.append(f"| {finding.severity}{advisory} | {outcome.platform} | {title} | {finding.category} |")
        if len(rows) > len(shown):
            lines.append(f"\n{len(rows) - len(shown)} more finding(s) are in the campaign report.")
        lines.append("")
        for finding, outcome in shown:
            lines.append(f"<details><summary><code>{finding.id}</code> {_cell(finding.title)}</summary>")
            lines.append("")
            if finding.steps:
                lines.append("Steps:")
                lines += [f"{index}. {step}" for index, step in enumerate(finding.steps[:12], start=1)]
                lines.append("")
            if finding.details:
                details = finding.details.strip()[:1500]
                if details.count("```") % 2:
                    details += "\n```"
                lines += [details, ""]
            if finding.suspected_sources:
                lines.append("Suspected sources: " + ", ".join(f"`{s}`" for s in finding.suspected_sources[:5]))
                lines.append("")
            image = evidence.get(finding.id)
            if image:
                lines.append(f"![{finding.id}]({image})")
                lines.append("")
            command = repro_command(finding, outcome)
            if command:
                lines += ["```sh", command, "```", ""]
            lines.append("</details>")
    if run_url:
        lines += ["", f"Evidence (screenshots, video, replays): {run_url}"]
    campaigns = [outcome.campaign_id for outcome in result.outcomes if outcome.campaign_id]
    if campaigns:
        lines += ["", "Campaigns: " + ", ".join(f"`{cid}`" for cid in campaigns)]
    mention = _HANDOFF.get(settings.handoff)
    if for_comment and mention and event.pr is not None and failing(result.findings, settings):
        lines += [
            "",
            f"{mention} please fix the non-advisory SwarmQA findings above on a new branch and open a PR. "
            "Start from the suspected sources. If you can run SwarmQA, confirm each fix with "
            "`aqa verify <finding id> --campaign <campaign>`; otherwise say the fix is unverified. "
            "SwarmQA tests your PR when you open it.",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")[:200]


def actions_run_url(env: Mapping[str, str]) -> str | None:
    if not env.get("GITHUB_RUN_ID") or not env.get("GITHUB_REPOSITORY"):
        return None
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    return f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"


class Reporter:
    """Posts to GitHub per `settings.report`; with `report = "none"` it posts nothing."""

    def __init__(
        self,
        client: GitHubClient | None,
        settings: GitHubSettings,
        *,
        log: Callable[[str], None] = print,
        env: Mapping[str, str] | None = None,
        runner: Runner | None = None,
    ):
        self.client = client
        self.settings = settings
        self.log = log
        self.env = os.environ if env is None else env
        self.runner = runner or build_runner
        self._check_ids: dict[str, int] = {}
        self._statuses_only = False

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.settings.report != "none"

    def begin(self, event: Event) -> None:
        if not (self.enabled and self.settings.posts_check):
            return
        try:
            self._check(event.sha, status="in_progress", title="Testing", summary=f"Testing {event.label()}")
        except GitHubError as exc:
            self.log(f"github: could not mark {event.sha[:12]} as in progress: {exc}")

    def finish(self, result: EventResult) -> list[str]:
        """Post the result; return the URLs posted to. Never raises for GitHub errors."""
        if not self.enabled:
            return []
        run_url = actions_run_url(self.env)
        evidence = self.upload_evidence(result)
        posted: list[str] = []
        state = conclusion(result, self.settings)
        title = headline(result, self.settings)
        comment_url = None
        if self.settings.posts_comment and result.event.pr is not None and not result.superseded:
            body = render(result, self.settings, evidence=evidence, run_url=run_url, for_comment=True)
            try:
                comment_url = self._comment(result.event.pr, body)
                posted.append(comment_url)
            except GitHubError as exc:
                self.log(f"github: could not comment on PR #{result.event.pr}: {exc}")
        if self.settings.posts_check:
            summary = render(result, self.settings, evidence=evidence, run_url=run_url)[:SUMMARY_LIMIT]
            try:
                url = self._check(result.event.sha, status="completed", conclusion=state, title=title,
                                  summary=summary, target_url=comment_url or run_url or result.event.url)
                if url:
                    posted.append(url)
            except GitHubError as exc:
                self.log(f"github: could not post the check for {result.event.sha[:12]}: {exc}")
        return posted

    def upload_evidence(self, result: EventResult) -> dict[str, str]:
        """Upload each listed finding's first screenshot with `evidence_command`."""
        command = self.settings.evidence_command
        if not command:
            return {}
        urls: dict[str, str] = {}
        rows = [(f, o) for o in result.outcomes for f in o.findings][: self.settings.max_findings]
        for finding, outcome in rows:
            images = [*finding.screenshots, *finding.evidence.frames]
            if not images or not outcome.report_dir:
                continue
            path = Path(outcome.report_dir) / images[0]
            if not path.is_file():
                continue
            done = run(self.runner, ["/bin/sh", "-c", command], env={"AQA_FILE": str(path)}, timeout=120)
            url = next((line.strip() for line in reversed((done.stdout or "").splitlines())
                        if line.strip().startswith("http")), None)
            if done.ok and url:
                urls[finding.id] = url
            else:
                self.log(f"github: evidence upload failed for {finding.id}: {done.detail()}")
        return urls

    # Posting -----------------------------------------------------------------

    def _check(self, sha: str, *, status: str, title: str, summary: str,
               conclusion: str | None = None, target_url: str | None = None) -> str | None:
        assert self.client is not None
        if not self._statuses_only:
            body = {"name": self.settings.check_name, "head_sha": sha, "status": status,
                    "output": {"title": title[:250], "summary": summary}}
            if conclusion:
                body["conclusion"] = conclusion
            try:
                existing = self._check_ids.get(sha)
                data = (self.client.update_check_run(existing, body) if existing
                        else self.client.create_check_run(body))
                self._check_ids[sha] = int(data["id"])
                return data.get("html_url")
            except GitHubError as exc:
                if exc.status != 403:
                    raise
                self.log("github: this token cannot create check runs (GitHub App only); "
                         "using a commit status instead")
                self._statuses_only = True
        state = {"success": "success", "failure": "failure", "neutral": "success"}.get(conclusion or "", "pending")
        body = {"state": state, "context": self.settings.check_name, "description": title[:140]}
        if target_url:
            body["target_url"] = target_url
        self.client.create_status(sha, body)
        return None

    def _comment(self, pr: int, body: str) -> str:
        assert self.client is not None
        existing = next((c for c in self.client.issue_comments(pr) if MARKER in str(c.get("body") or "")), None)
        data = (self.client.update_comment(int(existing["id"]), body) if existing
                else self.client.create_comment(pr, body))
        return str(data.get("html_url") or "")
