"""MCP tools: plain Python functions the MCP server exposes to agents.

The server registers each function in `TOOLS` under its own name and derives
the input schema from the type hints, so parameters stay JSON-friendly
(str, int, float, bool, lists and None). Results are dataclasses that the
server serialises with `swarmqa.serialize`. Every tool is safe to call from
an agent: `start_campaign` and `verify_fix` return at once with an id and
the work runs in a detached process.

Relative paths resolve against the server's project root and config (see
`swarmqa.mcp.campaigns.configure`). Errors an agent can act on (unknown
campaign or finding, bad arguments, invalid config) raise `AQAError` with a
message that says what to do next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from swarmqa.errors import AQAError
from swarmqa.models import CampaignStatus, Finding, FindingCategory, Severity

VerifyState = Literal["running", "passed", "failed", "error"]


@dataclass
class CampaignHandle:
    campaign_id: str
    report_dir: str


@dataclass
class FindingSummary:
    id: str
    title: str
    severity: Severity
    category: FindingCategory
    advisory: bool
    confidence: float
    campaign_id: str
    repro: str | None = None


@dataclass
class VerifyResult:
    """`passed` means the finding did not reproduce on any device."""

    verify_id: str
    finding_id: str
    state: VerifyState
    devices: int = 0
    reproduced_on: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    message: str = ""


def start_campaign(
    config_path: str = "aqa.config.toml",
    app: str | None = None,
    intents: list[str] | None = None,
    platform: Literal["macos", "ios"] | None = None,
    workers: int | None = None,
    sha: str | None = None,
) -> CampaignHandle:
    """Start a campaign in the background and return its id right away."""
    from swarmqa.intent.ingest import build_queue
    from swarmqa.mcp import campaigns
    from swarmqa.report.layout import ensure_campaign_layout
    from swarmqa.util import new_campaign_id

    root = campaigns.project_root()
    request = campaigns.CampaignRequest(
        config_path=str(campaigns.resolve_path(config_path or campaigns.DEFAULT_CONFIG, root)),
        cwd=str(root),
        app=app,
        intents=list(intents) if intents is not None else None,
        platform=platform,
        workers=workers,
        sha=sha,
    )
    if not Path(request.config_path).is_file():
        raise AQAError(f"config not found: {request.config_path}; run `aqa init` or pass config_path")
    # Fail here, not in the detached process, when the config or intents are bad.
    config = campaigns.load_request_config(request)
    queue = build_queue(config)
    if not queue:
        raise AQAError("the campaign has no shards; add intents or pass intents=[...]")
    report_root = Path(config.report_root)
    campaign_id = new_campaign_id()
    while (report_root / campaign_id).exists():
        campaign_id = new_campaign_id()
    campaign = (report_root / campaign_id).resolve()
    ensure_campaign_layout(campaign)
    campaigns.write_meta(
        campaign,
        {
            "campaign_id": campaign_id,
            "backend": config.backend,
            "workers": int(config.workers),
            "shards": len(queue),
            "sha": sha,
            "pr": None,
            "request": request.to_plain(),
            "created_at": campaigns.now(),
        },
    )
    try:
        pid = campaigns.launch_runner(campaign, root)
    except OSError as exc:
        # Record the failure the way a crashed runner would, so campaign_status
        # reports it instead of showing a campaign that is "running" forever.
        from swarmqa.serialize import dump_json

        now = campaigns.now()
        dump_json(
            {"started_at": now, "finished_at": now, "exit_code": 2, "error": f"could not start the runner: {exc}"},
            campaign / campaigns.RUNNER_RESULT,
        )
        raise AQAError(f"could not start campaign {campaign_id}: {exc}") from exc
    campaigns.update_meta(campaign, pid=pid)
    return CampaignHandle(campaign_id=campaign_id, report_dir=str(campaign))


def campaign_status(campaign_id: str | None = None) -> CampaignStatus:
    """Status of one campaign, or the latest when `campaign_id` is None."""
    from swarmqa.mcp import campaigns
    from swarmqa.orchestrator.status import load_status

    campaign = campaigns.campaign_path(campaign_id)
    meta = campaigns.read_meta(campaign)
    status = load_status(campaign)
    alive = campaigns.runner_alive(meta)
    # A runner result without a status means it ended (or never started) before writing one.
    ended = (campaign / campaigns.RUNNER_RESULT).is_file()
    if status is None:
        if meta and not ended and (alive or not meta.get("pid")):
            return CampaignStatus(
                campaign_id=campaign.name,
                state="running",
                backend=str(meta.get("backend") or "local"),
                workers_configured=int(meta.get("workers") or 0),
                queue_depth=int(meta.get("shards") or 0),
                report_dir=str(campaign),
            )
        if meta:
            raise AQAError(
                f"campaign {campaign.name} exited before it started; "
                f"see {campaign / campaigns.RUNNER_LOG}"
            )
        raise AQAError(f"campaign {campaign.name} has no status.json yet under {campaign}")
    if status.state == "running" and meta.get("pid") and not alive:
        # The runner is gone but never wrote a final status (killed or crashed).
        status.state = "stopped"
        status.active_workers = []
    if not status.report_dir:
        status.report_dir = str(campaign)
    return status


def list_findings(
    campaign_id: str | None = None,
    pr: int | None = None,
    include_advisory: bool = True,
    min_severity: Severity = "low",
) -> list[FindingSummary]:
    """Findings for a campaign, or for the latest campaign on a PR."""
    from swarmqa.mcp import campaigns

    threshold = _severity_rank(min_severity)
    if pr is not None:
        # Only campaigns whose mcp.json records this PR match. Nothing records
        # a PR before the GitHub watcher lands, so a pr filter returns [].
        matching = [
            path
            for path in campaigns.campaign_dirs()
            if campaigns.read_meta(path).get("pr") == pr
            and (not campaign_id or path.name == Path(campaign_id).name)
        ]
        if not matching:
            return []
        campaign = matching[-1]
    else:
        campaign = campaigns.campaign_path(campaign_id)
    summaries = [
        FindingSummary(
            id=finding.id,
            title=finding.title,
            severity=finding.severity,
            category=finding.category or "broken",
            advisory=bool(finding.advisory),
            confidence=float(finding.confidence),
            campaign_id=campaign.name,
            repro=finding.repro or finding.replay_json,
        )
        for finding in campaigns.load_campaign_findings(campaign)
        if (include_advisory or not finding.advisory) and _severity_rank(finding.severity) <= threshold
    ]
    summaries.sort(key=lambda item: (_severity_rank(item.severity), -item.confidence, item.id))
    return summaries


def get_finding(finding_id: str, campaign_id: str | None = None) -> Finding:
    """One finding in full, with evidence paths and the repro script."""
    from swarmqa.mcp import campaigns

    if campaign_id:
        search = [campaigns.campaign_path(campaign_id)]
    else:
        search = list(reversed(campaigns.campaign_dirs()))
        if not search:
            raise AQAError(f"no campaigns in {campaigns.report_root()}; start one with start_campaign")
    for campaign in search:
        for finding in campaigns.load_campaign_findings(campaign):
            if finding.id == finding_id:
                return finding
    where = f"campaign {search[0].name}" if campaign_id else "any campaign"
    raise AQAError(f"unknown finding {finding_id!r} in {where}; call list_findings for valid ids")


def verify_fix(
    finding_id: str,
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
) -> VerifyResult:
    """Rebuild (unless `build` is False) and replay the finding's repro on `devices` devices.

    Returns at once with state `running`; poll with `verify_status`.
    """
    from swarmqa import verify
    from swarmqa.mcp import campaigns

    if devices < 1:
        raise AQAError("devices must be at least 1")
    if campaign_id:
        campaigns.campaign_path(campaign_id)
    return verify.start_verify(
        finding_id,
        config_path=campaigns.config_path(),
        campaign_id=campaign_id,
        devices=devices,
        build=build,
    )


def verify_status(verify_id: str) -> VerifyResult:
    """Current state of a `verify_fix` run."""
    from swarmqa import verify
    from swarmqa.mcp import campaigns

    return verify.verify_status(verify_id, report_root=campaigns.report_root())


def cancel_campaign(campaign_id: str, drain: bool = True) -> CampaignStatus:
    """Stop a campaign. `drain` lets running shards finish; otherwise they are cancelled."""
    from swarmqa.mcp import campaigns
    from swarmqa.orchestrator.status import load_status, write_status

    campaign = campaigns.campaign_path(campaign_id)
    meta = campaigns.read_meta(campaign)
    pid = meta.get("pid")
    if not pid:
        raise AQAError(
            f"campaign {campaign.name} was not started by the MCP server; "
            "stop it from the terminal that runs `aqa run`"
        )
    if not campaigns.runner_alive(meta):
        return campaign_status(campaign.name)
    campaigns.request_cancel(campaign, drain=drain)
    campaigns.update_meta(campaign, cancel_requested_at=campaigns.now(), cancel_drain=bool(drain))
    if drain:
        return campaign_status(campaign.name)
    campaigns.terminate_runner(int(pid))
    status = load_status(campaign)
    if status is None:
        status = CampaignStatus(
            campaign_id=campaign.name,
            state="stopped",
            backend=str(meta.get("backend") or "local"),
            workers_configured=int(meta.get("workers") or 0),
            report_dir=str(campaign),
        )
    elif status.state == "running":
        interrupted = [worker.shard_id for worker in status.active_workers if worker.shard_id]
        status.cancelled = list(dict.fromkeys([*status.cancelled, *interrupted]))
        status.active_workers = []
        status.state = "stopped"
    write_status(status, campaign)
    return status


_SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")


def _severity_rank(severity: str) -> int:
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        raise AQAError(f"unknown severity {severity!r}; use one of {', '.join(_SEVERITY_ORDER)}") from None


TOOLS = (
    start_campaign,
    campaign_status,
    list_findings,
    get_finding,
    verify_fix,
    verify_status,
    cancel_campaign,
)
