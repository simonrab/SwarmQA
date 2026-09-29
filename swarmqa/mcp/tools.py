"""MCP tool signatures. Stubs until the MCP server lands.

The server registers each function in `TOOLS` under its own name and derives
the input schema from the type hints, so parameters stay JSON-friendly
(str, int, float, bool, lists and None). Results are dataclasses that the
server serialises with `swarmqa.serialize`. Every tool is safe to call from
an agent: `start_campaign` and `verify_fix` return at once with an id and
the work runs in a detached process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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
    raise NotImplementedError("start_campaign lands with the MCP server (WP-C1)")


def campaign_status(campaign_id: str | None = None) -> CampaignStatus:
    """Status of one campaign, or the latest when `campaign_id` is None."""
    raise NotImplementedError("campaign_status lands with the MCP server (WP-C1)")


def list_findings(
    campaign_id: str | None = None,
    pr: int | None = None,
    include_advisory: bool = True,
    min_severity: Severity = "low",
) -> list[FindingSummary]:
    """Findings for a campaign, or for the latest campaign on a PR."""
    raise NotImplementedError("list_findings lands with the MCP server (WP-C1)")


def get_finding(finding_id: str, campaign_id: str | None = None) -> Finding:
    """One finding in full, with evidence paths and the repro script."""
    raise NotImplementedError("get_finding lands with the MCP server (WP-C1)")


def verify_fix(
    finding_id: str,
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
) -> VerifyResult:
    """Rebuild (unless `build` is False) and replay the finding's repro on `devices` devices.

    Returns at once with state `running`; poll with `verify_status`.
    """
    raise NotImplementedError("verify_fix lands with WP-C2")


def verify_status(verify_id: str) -> VerifyResult:
    """Current state of a `verify_fix` run."""
    raise NotImplementedError("verify_status lands with WP-C2")


def cancel_campaign(campaign_id: str, drain: bool = True) -> CampaignStatus:
    """Stop a campaign. `drain` lets running shards finish; otherwise they are cancelled."""
    raise NotImplementedError("cancel_campaign lands with the MCP server (WP-C1)")


TOOLS = (
    start_campaign,
    campaign_status,
    list_findings,
    get_finding,
    verify_fix,
    verify_status,
    cancel_campaign,
)
