"""Re-run findings before they are reported.

Each eligible finding is replayed `confirm_runs` times with
`report.repro.replay_finding`, each time on a new driver (a fresh launch) in
`<campaign>/confirm/<finding-id>-<n>/`. The replay rule is the one `aqa
replay` and `aqa verify` use. An attempt the replay could not run (the app
did not launch, the driver could not be made) does not count.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from swarmqa.confirm.group import rank
from swarmqa.confirm.settings import FindingsSettings
from swarmqa.models import CampaignConfig, Finding
from swarmqa.report import repro
from swarmqa.report.repro import replay_finding, replay_path_for
from swarmqa.reporter.findings import write_replay

FLAKY_CONFIDENCE = 0.6
NOT_REPRODUCED_CONFIDENCE = 0.3
_now = time.monotonic


@dataclass
class Attempt:
    ran: bool
    reproduced: bool
    reason: str


def eligible(finding: Finding, settings: FindingsSettings) -> bool:
    """Whether settings ask for this finding to be re-run."""
    if finding.kind not in settings.confirm_kinds:
        return False
    return not (settings.skip_advisory and finding.advisory)


def confirm_findings(
    findings: list[Finding],
    campaign_dir: Path,
    config: CampaignConfig,
    settings: FindingsSettings,
    driver_factory: Callable | None = None,
) -> None:
    """Re-run eligible findings in place, most severe first, within the budget.

    Sets `confirmation`, `confirm_attempts` and `confirm_hits`, and lowers
    `confidence` (and sets `advisory` when the finding never showed again).
    Findings past `max_confirm` or the budget keep `confirmation = None`.
    """
    factory = _unleased(driver_factory) or repro._default_driver_factory(config)
    queue = sorted((item for item in findings if eligible(item, settings)), key=rank)[: settings.max_confirm]
    deadline = _now() + float(settings.confirm_budget_s)
    for finding in queue:
        if _now() >= deadline:
            break
        replay = _replay_for(campaign_dir, finding)
        if replay is None:
            _settle(finding, [], "no replay flow")
            continue
        attempts: list[Attempt] = []
        for number in range(1, settings.confirm_runs + 1):
            if attempts and _now() >= deadline:
                break
            work_dir = Path(campaign_dir) / "confirm" / f"{finding.id}-{number}"
            attempts.append(_attempt(replay, finding, config, factory, work_dir))
        _settle(finding, attempts, "the replay could not run")


def _unleased(driver_factory: Callable | None) -> Callable | None:
    """A swarm factory wrapping an injected one: call the injected one directly.

    The workers are done, so an injected factory needs no lease. A swarm
    factory that builds real drivers keeps leasing a device per re-run.
    """
    from swarmqa.orchestrator.swarm import LeasingDriverFactory

    if isinstance(driver_factory, LeasingDriverFactory) and driver_factory.base is not None:
        return driver_factory.base
    return driver_factory


def _replay_for(campaign_dir: Path, finding: Finding) -> Path | None:
    path = replay_path_for(campaign_dir, finding)
    if path.is_file():
        return path
    if finding.kind == "launch":
        return write_replay(finding.id, Path(campaign_dir) / "confirm" / finding.id, [{"action": "launch"}])
    return None


def _attempt(replay: Path, finding: Finding, config: CampaignConfig, factory: Callable, work_dir: Path) -> Attempt:
    driver = None
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        driver = factory(config.app, work_dir)
        result = replay_finding(replay, config, driver=driver, work_dir=work_dir, finding=finding)
        return Attempt(bool(result.ran), bool(result.ran and result.reproduced), result.reason)
    except Exception as exc:  # noqa: BLE001 - one broken re-run must not stop the rest
        return Attempt(False, False, f"{type(exc).__name__}: {exc}")
    finally:
        _close(driver)


def _close(driver: Any) -> None:
    close = getattr(driver, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - already closed
        return


def _settle(finding: Finding, attempts: list[Attempt], why_skipped: str) -> None:
    ran = [item for item in attempts if item.ran]
    hits = sum(item.reproduced for item in ran)
    finding.confirm_attempts = len(ran)
    finding.confirm_hits = hits
    if not ran:
        finding.confirmation = "skipped"
        reason = attempts[-1].reason if attempts else why_skipped
        _note(finding, f"Not re-run: {reason}.")
        return
    if hits == len(ran):
        finding.confirmation = "confirmed"
        _note(finding, f"Confirmed: showed again on {hits} of {len(ran)} re-run(s).")
    elif hits:
        finding.confirmation = "flaky"
        finding.confidence = min(float(finding.confidence), FLAKY_CONFIDENCE)
        _note(finding, f"Flaky: showed again on {hits} of {len(ran)} re-run(s).")
    else:
        finding.confirmation = "not_reproduced"
        finding.advisory = True
        finding.confidence = min(float(finding.confidence), NOT_REPRODUCED_CONFIDENCE)
        _note(finding, f"Not reproduced on {len(ran)} re-run(s) ({ran[-1].reason}); now advisory.")


def _note(finding: Finding, text: str) -> None:
    finding.details = f"{finding.details}\n\n{text}" if finding.details else text
