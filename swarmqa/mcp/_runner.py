"""Detached campaign runner started by the MCP `start_campaign` tool.

Usage: `python -m swarmqa.mcp._runner <campaign_dir>`. Reads the request in
`<campaign_dir>/mcp.json`, rebuilds the config and queue the way `aqa run`
does, and runs the campaign under the id `start_campaign` chose. Writes
`raw/mcp-runner.json` with the exit code when it ends.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from swarmqa.budgets import BudgetDecision, CampaignClock
from swarmqa.mcp import campaigns

CANCEL_STOP_REASON = "cancelled"


def cancellable_clock(campaign: Path) -> type[CampaignClock]:
    """A CampaignClock that stops scheduling once `cancel-request.json` exists."""

    class _CancellableClock(CampaignClock):
        def can_schedule(self, elapsed_s: float) -> BudgetDecision:
            if campaigns.cancel_requested(campaign):
                self.stop_reason = CANCEL_STOP_REASON
                return BudgetDecision(False, stop_reason=CANCEL_STOP_REASON)
            return super().can_schedule(elapsed_s)

    return _CancellableClock


def run(campaign: Path) -> int:
    from swarmqa.intent.ingest import build_queue
    from swarmqa.models import RunOptions
    from swarmqa.orchestrator import campaign as orchestrator

    campaign = Path(campaign).resolve()
    meta = campaigns.read_meta(campaign)
    request = campaigns.CampaignRequest.from_meta(meta)
    config = campaigns.load_request_config(request)
    queue = build_queue(config)
    # No status.json yet, so "resume" finds nothing and starts a fresh
    # campaign under the id start_campaign chose.
    options = RunOptions(campaign_id=campaign.name, partial=bool(request.intents))
    # Drain support without touching the orchestrator: its module-level
    # CampaignClock is swapped for one that honours cancel-request.json.
    orchestrator.CampaignClock = cancellable_clock(campaign)  # type: ignore[misc]
    result = orchestrator.run_campaign(config, queue, options=options)
    return int(result.exit_code)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m swarmqa.mcp._runner <campaign_dir>", file=sys.stderr)
        return 2
    campaign = Path(args[0]).resolve()
    record = {"started_at": campaigns.now()}
    try:
        code = run(campaign)
        record.update(exit_code=code)
    except Exception as exc:  # noqa: BLE001 - reported through the result file
        traceback.print_exc()
        code = 2
        record.update(exit_code=code, error=f"{type(exc).__name__}: {exc}")
    record["finished_at"] = campaigns.now()
    try:
        from swarmqa.serialize import dump_json

        dump_json(record, campaign / campaigns.RUNNER_RESULT)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
