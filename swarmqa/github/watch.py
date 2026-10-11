"""The GitHub trigger: find PR heads and default-branch merges, build each SHA,
run one campaign per platform, and report back.

`aqa watch` polls with `gh api`. Processed SHAs live in
`~/.aqa/state/<owner>-<name>.json`, so a restart never re-tests a SHA. The
first continuous poll of a repo only records what is already there (open PR
heads and the branch head) and tests what changes after that; `--pr`,
`--latest-pr` and `--latest-merged` test one event right away, seen or not.

One campaign runs per SHA and platform. A PR whose head moves on before its
run starts is skipped as superseded (the next poll picks up the new head);
one that moves on during the run still reports on its own SHA but leaves
the PR comment to the newer run.
"""

from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import Any, Callable

from swarmqa.github.client import GitHubClient, GitHubError, PullRequest, detect_repo
from swarmqa.github.events import Event, EventResult, PlatformOutcome
from swarmqa.github.report import Reporter, render
from swarmqa.github.settings import GitHubSettings
from swarmqa.models import CampaignConfig
from swarmqa.serialize import dump_json, load_json

Log = Callable[[str], None]


class WatchState:
    """`~/.aqa/state/<owner>-<name>.json`: SHAs seen, PR heads and branch heads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.exists = self.path.is_file()
        data: dict[str, Any] = {}
        if self.exists:
            try:
                data = load_json(self.path)
            except (OSError, ValueError):
                data = {}
        self.shas: dict[str, dict[str, Any]] = dict(data.get("shas") or {})
        self.prs: dict[str, str] = dict(data.get("prs") or {})
        self.branches: dict[str, str] = dict(data.get("branches") or {})

    @classmethod
    def for_repo(cls, settings: GitHubSettings, repo: str) -> "WatchState":
        return cls(settings.state_dir() / f"{repo.replace('/', '-')}.json")

    def seen(self, sha: str) -> bool:
        return sha in self.shas

    def record(self, sha: str, **fields: Any) -> None:
        self.shas.setdefault(sha, {}).update(fields, updated_at=_now())
        self.save()

    def save(self) -> None:
        dump_json({"shas": self.shas, "prs": self.prs, "branches": self.branches}, self.path)
        self.exists = True


def pr_event(pull: PullRequest) -> Event:
    return Event("pr", pull.head_sha, pull.number, pull.head_ref, pull.title, pull.url, pull.base_ref)


def poll(client: GitHubClient, settings: GitHubSettings, state: WatchState, branch: str,
         *, save: bool = True) -> list[Event]:
    """New events since the last poll; the first poll of a repo only records a baseline.

    With `save=False` (a dry run) the state file is left alone."""
    baseline = not state.exists
    events: list[Event] = []
    if "pr" in settings.events:
        for pull in client.open_pulls():
            if baseline:
                state.prs[str(pull.number)] = pull.head_sha
                state.shas.setdefault(pull.head_sha, {"state": "baseline", "pr": pull.number})
            elif not state.seen(pull.head_sha):
                events.append(pr_event(pull))
    if "push" in settings.events:
        head = client.branch_head(branch)
        if baseline or branch not in state.branches:
            state.branches[branch] = head
            state.shas.setdefault(head, {"state": "baseline", "ref": branch})
        elif head != state.branches[branch]:
            state.branches[branch] = head
            if not state.seen(head):
                events.append(Event("push", head, None, branch, f"{branch} @ {head[:12]}"))
    if save:
        state.save()
    return events


def build_repo(config: CampaignConfig, settings: GitHubSettings, repo: str | None) -> str | None:
    """Where the builder clones from: github.clone_url, build.repo, GitHub, then app.source_dir.

    GitHub comes before app.source_dir because a local checkout usually lacks
    other people's PR heads.
    """
    from swarmqa.build.settings import BuildSettings

    explicit = settings.clone_url or BuildSettings.from_mapping(config.build.settings).repo
    if explicit:
        return explicit
    if repo:
        return f"https://github.com/{repo}.git"
    return config.app.source_dir


def use_gh_credentials(env: dict[str, str] | None = None) -> None:
    """Let git clone private https://github.com repos with the `gh` login.

    Adds `gh auth git-credential` as the credential helper through
    GIT_CONFIG_* variables, which the builder's git calls inherit, unless
    the environment already sets git config that way.
    """
    env = os.environ if env is None else env
    if "GIT_CONFIG_COUNT" in env:
        return
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.helper"
    env["GIT_CONFIG_VALUE_0"] = "!gh auth git-credential"


def evaluate_sha(
    event: Event,
    config: CampaignConfig,
    settings: GitHubSettings,
    *,
    repo: str | None = None,
    builder: Any = None,
    run_campaign: Callable | None = None,
    diff_intents: Callable[..., list[Path]] | None = None,
    log: Log = print,
) -> EventResult:
    """Build `event.sha` for each platform and run a campaign on each build.

    With `[flows] from_diff = true`, intents proposed from the change are
    added to every platform's campaign (computed once per event).
    """
    from swarmqa.build.builder import ShaBuilder
    from swarmqa.build.findings import build_failure_finding
    from swarmqa.build.settings import BuildSettings
    from swarmqa.flows.settings import FlowsSettings
    from swarmqa.intent.ingest import build_queue
    from swarmqa.models import RunOptions
    from swarmqa.report.findings_json import FINDINGS_JSON, load_findings_json

    if run_campaign is None:
        from swarmqa.orchestrator.campaign import run_campaign
    source = build_repo(config, settings, repo)
    if source and source.startswith("https://github.com/"):
        use_gh_credentials()
    builder = builder or ShaBuilder(BuildSettings.from_config(config))
    from_diff = FlowsSettings.from_config(config).from_diff
    diff_dir: str | None = None
    result = EventResult(event)
    for platform in settings.platforms:
        outcome = PlatformOutcome(platform)
        result.outcomes.append(outcome)
        log(f"{event.label()}: building {platform}")
        built = builder.build_result(event.sha, platform, repo=source)
        if built.sha and len(built.sha) > len(event.sha):
            event.sha = built.sha
        if built.failure is not None:
            outcome.findings = [build_failure_finding(built.failure)]
            outcome.error = f"build failed: {built.failure.summary()}"
            outcome.exit_code = 1
            log(f"{event.label()}: {platform} {outcome.error.splitlines()[0]}")
            continue
        assert built.artifact is not None
        campaign_config = copy.deepcopy(config)
        campaign_config.app.platform = platform
        campaign_config.app.path = built.artifact.app_path
        campaign_config.app.bundle_id = built.artifact.bundle_id
        if from_diff:
            if diff_dir is None:
                diff_dir = _diff_intents(event, config, source, diff_intents, log)
            if diff_dir:
                campaign_config.intents = [*campaign_config.intents, diff_dir]
        try:
            queue = build_queue(campaign_config)
            if not queue:
                outcome.error = "no shards: add intents to the config"
                outcome.exit_code = 2
                continue
            log(f"{event.label()}: running {len(queue)} shard(s) on {platform}")
            ran = run_campaign(campaign_config, queue, options=RunOptions())
        except Exception as exc:  # noqa: BLE001 - one platform failing must not stop the report
            outcome.error = f"campaign failed: {type(exc).__name__}: {exc}"
            outcome.exit_code = 2
            log(f"{event.label()}: {platform} {outcome.error}")
            continue
        outcome.campaign_id = ran.campaign_id
        outcome.report_dir = ran.report_dir
        outcome.exit_code = int(ran.exit_code)
        findings_path = Path(ran.report_dir) / FINDINGS_JSON
        if findings_path.is_file():
            outcome.findings = load_findings_json(findings_path)
    return result


def _diff_intents(
    event: Event,
    config: CampaignConfig,
    source: str | None,
    propose: Callable[..., list[Path]] | None,
    log: Log,
) -> str:
    """The directory of intents proposed from `event`'s change, or "" when there are none.

    The diff is read from the builder's mirror, whose HEAD is the default
    branch: a PR compares its base (or HEAD) with the head SHA, a push its
    first parent. A failure is logged and the run goes on with the
    configured intents only.
    """
    out = Path(config.report_root) / "diff-intents" / event.sha[:12]
    base = event.base or ("HEAD" if event.kind == "pr" else f"{event.sha}^")
    try:
        if propose is None:
            from swarmqa.build.checkout import RepoCache
            from swarmqa.build.settings import BuildSettings
            from swarmqa.flows.from_diff import intents_from_diff

            if not source:
                raise ValueError("no repo to read the diff from")
            mirror = RepoCache(source, BuildSettings.from_config(config)).mirror
            paths = intents_from_diff(config, mirror, base, event.sha, out, log=log)
        else:
            paths = propose(event, config, source, out)
    except Exception as exc:  # noqa: BLE001 - the configured intents still run
        log(f"{event.label()}: flows from the diff skipped: {type(exc).__name__}: {exc}")
        return ""
    return str(out) if paths else ""


class Watcher:
    def __init__(
        self,
        config: CampaignConfig,
        settings: GitHubSettings,
        client: GitHubClient,
        *,
        reporter: Reporter | None = None,
        state: WatchState | None = None,
        tester: Callable[..., EventResult] = evaluate_sha,
        log: Log = print,
    ):
        self.config = config
        self.settings = settings
        self.client = client
        self.reporter = reporter or Reporter(client, settings, log=log)
        self.state = state or WatchState.for_repo(settings, client.repo)
        self.tester = tester
        self.log = log

    def _head(self, pr: int) -> str | None:
        try:
            return self.client.pull(pr).head_sha
        except GitHubError as exc:
            self.log(f"github: could not read PR #{pr}: {exc}")
            return None

    def process(self, event: Event) -> EventResult:
        if event.pr is not None:
            head = self._head(event.pr)
            if head and head != event.sha:
                self.log(f"{event.label()}: superseded by {head[:12]} before it started")
                self.state.record(event.sha, state="superseded", pr=event.pr)
                return EventResult(event, superseded=True)
        self.state.record(event.sha, state="running", kind=event.kind, pr=event.pr, ref=event.ref)
        self.reporter.begin(event)
        result = self.tester(event, self.config, self.settings, repo=self.client.repo, log=self.log)
        if event.pr is not None:
            head = self._head(event.pr)
            result.superseded = bool(head and head != event.sha)
            self.state.prs[str(event.pr)] = event.sha
        elif event.ref:
            self.state.branches[event.ref] = event.sha
        self.log(render(result, self.settings))
        result.posted = self.reporter.finish(result)
        self.state.record(
            event.sha,
            state="superseded" if result.superseded else "done",
            campaigns=[o.campaign_id for o in result.outcomes if o.campaign_id],
            findings=len(result.findings),
            exit_code=result.exit_code,
            posted=result.posted,
        )
        return result

    def watch(self, *, once: bool = False, interval: float | None = None,
              sleep: Callable[[float], None] = time.sleep, polls: int | None = None) -> int:
        branch = self.settings.branch or self.client.default_branch()
        delay = interval or self.settings.poll_interval_s
        baseline = not self.state.exists
        count = 0
        self.log(f"watching {self.client.repo} ({', '.join(self.settings.events)}; branch {branch}; "
                 f"report {self.settings.report})")
        while True:
            try:
                events = poll(self.client, self.settings, self.state, branch)
            except GitHubError as exc:
                self.log(f"github: poll failed: {exc}")
                events = []
            if baseline:
                self.log(f"first poll of {self.client.repo}: recorded the current heads; "
                         "testing what changes from now on")
                baseline = False
            for event in events:
                self.process(event)
            count += 1
            if once or (polls is not None and count >= polls):
                return 0
            sleep(delay)


def resolve_repo(config: CampaignConfig, settings: GitHubSettings, runner=None) -> str | None:
    return (settings.repo or os.environ.get("GITHUB_REPOSITORY")
            or detect_repo(runner, cwd=config.app.source_dir or None))


def run_sha(
    config: CampaignConfig,
    sha: str,
    *,
    pr: int | None = None,
    clone_url: str | None = None,
    client: GitHubClient | None = None,
    tester: Callable[..., EventResult] = evaluate_sha,
    log: Log = print,
) -> int:
    """`aqa run --github-sha`: test one SHA and report it like `aqa watch` would."""
    settings = GitHubSettings.from_config(config)
    if clone_url:
        settings.clone_url = clone_url
    if client is None:
        repo = resolve_repo(config, settings)
        client = GitHubClient(repo) if repo else None
    if client is None and settings.report != "none":
        log("github: no repo (set github.repo); results will not be posted")
    event = Event("pr" if pr is not None else "push", sha, pr)
    if client is not None and pr is None:
        try:
            pulls = [p for p in client.pulls_for_commit(sha) if p.state == "open" and p.head_sha == sha]
        except GitHubError as exc:
            log(f"github: could not look up PRs for {sha[:12]}: {exc}")
            pulls = []
        if pulls:
            event = pr_event(pulls[0])
    reporter = Reporter(client, settings, log=log)
    reporter.begin(event)
    result = tester(event, config, settings, repo=client.repo if client else None, log=log)
    log(render(result, settings))
    for url in reporter.finish(result):
        log(f"posted {url}")
    return result.exit_code


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
