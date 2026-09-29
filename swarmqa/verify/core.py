"""The in-process verify run behind `run_verify` and `aqa verify`.

1. Resolve the campaign (the latest when none is named), the finding in its
   `findings.json`, and the finding's replay flow.
2. Build (see `build.py`), unless told not to.
3. Replay the flow on N devices with `report.repro.replay_finding`, each in
   `<verify dir>/devices/<n>-<name>/`.
4. Verdict: `failed` when any device reproduced the finding, else `error`
   when any device could not run the replay, else `passed`.

Devices: for `app.platform = "ios"` with `app.simulators`, device n uses
simulator n (wrapping round, so two slots on one simulator run one after
the other). Otherwise every device is the host: parallel with the fake
driver or an injected `driver_factory`, one at a time with a real driver,
because the local Mac has one display and one copy of the app.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from swarmqa.errors import AQAError
from swarmqa.mcp.tools import VerifyResult
from swarmqa.models import AppTarget, CampaignConfig, Finding
from swarmqa.orchestrator.status import resolve_campaign_dir
from swarmqa.report.findings_json import FINDINGS_JSON, load_findings_json
from swarmqa.report.paths import public
from swarmqa.report.repro import ReplayResult, replay_finding, replay_path_for
from swarmqa.reporter.findings import write_replay
from swarmqa.serialize import dump_json
from swarmqa.verify import build as build_mod
from swarmqa.verify.status import new_verify_id, verify_dir, write_status

DriverFactory = Callable[[Any, Path], Any]
_EVIDENCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".mp4", ".mov", ".json"}


class VerifySetupError(AQAError):
    """The campaign, finding or replay could not be found."""


@dataclass
class DeviceSlot:
    index: int
    name: str
    target: AppTarget
    lock_key: str


@dataclass
class DeviceOutcome:
    """One device's replay. `state` is `reproduced`, `clear` or `error`."""

    device: str
    state: str
    reason: str
    work_dir: str = ""
    evidence: list[str] = field(default_factory=list)
    duration_s: float = 0.0


@dataclass
class VerifyRun:
    result: VerifyResult
    devices: list[DeviceOutcome] = field(default_factory=list)
    campaign_dir: Path | None = None
    verify_dir: Path | None = None


@dataclass
class Prepared:
    campaign_dir: Path
    finding: Finding
    replay: Path | None


# Setup ----------------------------------------------------------------------


def prepare(config: CampaignConfig, finding_id: str, campaign_id: str | None) -> Prepared:
    """Find the campaign and the finding. Raises VerifySetupError."""
    try:
        campaign_dir = resolve_campaign_dir(Path(config.report_root), campaign_id)
    except AQAError as exc:
        raise VerifySetupError(str(exc)) from exc
    if not campaign_dir.is_dir():
        raise VerifySetupError(f"campaign not found: {campaign_dir}")
    path = campaign_dir / FINDINGS_JSON
    if not path.is_file():
        raise VerifySetupError(f"no {FINDINGS_JSON} in {campaign_dir}")
    try:
        findings = load_findings_json(path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise VerifySetupError(f"could not read {path}: {exc}") from exc
    finding = next((item for item in findings if item.id == finding_id), None)
    if finding is None:
        raise VerifySetupError(f"finding {finding_id} not found in campaign {campaign_dir.name}")
    replay = _replay_for(campaign_dir, finding)
    if replay is None and finding.kind != "launch":
        raise VerifySetupError(f"finding {finding_id} has no replay flow to verify with")
    return Prepared(campaign_dir, finding, replay)


def _replay_for(campaign_dir: Path, finding: Finding) -> Path | None:
    for candidate in (finding.repro, finding.replay_json):
        if candidate:
            path = replay_path_for(campaign_dir, replace(finding, replay_json=candidate))
            if path.is_file():
                return path
    fallback = replay_path_for(campaign_dir, finding)
    return fallback if fallback.is_file() else None


def plan_devices(config: CampaignConfig, count: int, *, injected: bool = False) -> list[DeviceSlot]:
    """Which device each of the `count` replays runs on, and which ones must not overlap."""
    app = config.app
    simulators = list(dict.fromkeys(item.strip() for item in app.simulators if item.strip()))
    slots: list[DeviceSlot] = []
    if app.platform == "ios" and simulators:
        for index in range(count):
            simulator = simulators[index % len(simulators)]
            name = simulator if count <= len(simulators) else f"{simulator}#{index // len(simulators) + 1}"
            target = replace(app, simulators=[simulator], simulator=simulator, launch_args=list(app.launch_args),
                             env=dict(app.env))
            slots.append(DeviceSlot(index, name, target, lock_key=f"sim:{simulator}"))
        return slots
    parallel = injected or config.driver.kind == "fake"
    for index in range(count):
        name = f"{app.platform}-{index + 1}"
        key = f"slot:{index}" if parallel else "host"
        slots.append(DeviceSlot(index, name, copy.deepcopy(app), lock_key=key))
    return slots


# The run --------------------------------------------------------------------


def verify(
    finding_id: str,
    *,
    config: CampaignConfig,
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
    verify_id: str | None = None,
    driver_factory: DriverFactory | None = None,
    builder: build_mod.Builder | None = None,
) -> VerifyRun:
    """Do the whole verify run and return the result with per-device detail."""
    verify_id = verify_id or new_verify_id()
    count = int(devices)
    if count < 1:
        return VerifyRun(VerifyResult(verify_id, finding_id, "error", message="devices must be at least 1"))
    try:
        prepared = prepare(config, finding_id, campaign_id)
    except VerifySetupError as exc:
        return VerifyRun(VerifyResult(verify_id, finding_id, "error", devices=count, message=str(exc)))

    directory = verify_dir(prepared.campaign_dir, verify_id)
    run = VerifyRun(VerifyResult(verify_id, finding_id, "running", devices=count),
                    campaign_dir=prepared.campaign_dir, verify_dir=directory)
    base = dict(campaign_id=prepared.campaign_dir.name, pid=os.getpid())
    write_status(directory, run.result, phase="build" if build else "replay", started_at=time.time(), **base)
    try:
        _run(run, prepared, config, count, build, driver_factory, builder or build_mod.build_app)
    except Exception as exc:  # noqa: BLE001 - never leave status.json at running
        run.result.state = "error"
        run.result.message = f"verify crashed: {type(exc).__name__}: {exc}"
    write_status(directory, run.result, phase="done", finished_at=time.time(),
                 device_results=run.devices, **base)
    return run


def _run(
    run: VerifyRun,
    prepared: Prepared,
    config: CampaignConfig,
    count: int,
    build: bool,
    driver_factory: DriverFactory | None,
    builder: build_mod.Builder,
) -> None:
    directory = run.verify_dir
    assert directory is not None
    notes: list[str] = []
    if build:
        outcome = builder(config, directory / "build.log")
        if outcome.log:
            run.result.evidence.append(public(prepared.campaign_dir, outcome.log))
        if not outcome.ok:
            run.result.state = "error"
            run.result.message = outcome.message
            return
        if outcome.skipped:
            notes.append(outcome.message)
    else:
        notes.append("build skipped (--no-build)")
    write_status(directory, run.result, phase="replay")

    replay = prepared.replay or write_replay(prepared.finding.id, directory, [{"action": "launch"}])
    slots = plan_devices(config, count, injected=driver_factory is not None)
    locks: dict[str, threading.Lock] = {slot.lock_key: threading.Lock() for slot in slots}
    status_lock = threading.Lock()
    outcomes: list[DeviceOutcome | None] = [None] * len(slots)

    def work(slot: DeviceSlot) -> None:
        with locks[slot.lock_key]:
            outcome = _replay_on(slot, replay, prepared, config, directory, driver_factory)
        outcomes[slot.index] = outcome
        with status_lock:
            write_status(directory, run.result, device_results=[item for item in outcomes if item is not None])

    with ThreadPoolExecutor(max_workers=len(slots), thread_name_prefix="verify") as pool:
        list(pool.map(work, slots))

    run.devices = [item for item in outcomes if item is not None]
    for item in run.devices:
        run.result.evidence.extend(item.evidence)
    _verdict(run, notes)


def _replay_on(
    slot: DeviceSlot,
    replay: Path,
    prepared: Prepared,
    config: CampaignConfig,
    directory: Path,
    driver_factory: DriverFactory | None,
) -> DeviceOutcome:
    work_dir = directory / "devices" / f"{slot.index + 1}-{_safe(slot.name)}"
    work_dir.mkdir(parents=True, exist_ok=True)
    device_config = copy.deepcopy(config)
    device_config.app = slot.target
    started = time.monotonic()
    driver = None
    try:
        factory = driver_factory or _configured_driver_factory(device_config)
        driver = factory(slot.target, work_dir)
        result = replay_finding(replay, device_config, driver=driver, work_dir=work_dir, finding=prepared.finding)
        outcome = _outcome(slot.name, result)
    except Exception as exc:  # noqa: BLE001 - one broken device must not sink the others
        outcome = DeviceOutcome(slot.name, "error", f"could not replay: {type(exc).__name__}: {exc}")
        result = None
    finally:
        _safe_close(driver)
    outcome.duration_s = round(time.monotonic() - started, 3)
    outcome.work_dir = public(prepared.campaign_dir, work_dir)
    summary = work_dir / "replay_result.json"
    dump_json({"device": slot.name, "outcome": outcome, "replay": _replay_summary(result)}, summary)
    outcome.evidence = _evidence(prepared.campaign_dir, work_dir)
    return outcome


def _outcome(device: str, result: ReplayResult) -> DeviceOutcome:
    if result.reproduced:
        return DeviceOutcome(device, "reproduced", result.reason)
    if not result.ran:
        return DeviceOutcome(device, "error", f"could not replay: {result.reason}")
    return DeviceOutcome(device, "clear", result.reason)


def _verdict(run: VerifyRun, notes: list[str]) -> None:
    reproduced = [item for item in run.devices if item.state == "reproduced"]
    broken = [item for item in run.devices if item.state == "error"]
    run.result.reproduced_on = [item.device for item in reproduced]
    if reproduced:
        run.result.state = "failed"
        reasons = "; ".join(f"{item.device}: {item.reason}" for item in reproduced)
        lines = [f"still reproduces on {len(reproduced)} of {len(run.devices)} device(s) ({reasons})"]
    elif broken or len(run.devices) < run.result.devices:
        run.result.state = "error"
        reasons = "; ".join(f"{item.device}: {item.reason}" for item in broken)
        lines = [f"could not verify on {len(broken)} of {run.result.devices} device(s) ({reasons})"]
    else:
        run.result.state = "passed"
        lines = [f"did not reproduce on {len(run.devices)} device(s)"]
    run.result.message = "\n".join(lines + notes)


# Helpers --------------------------------------------------------------------


def _configured_driver_factory(config: CampaignConfig) -> DriverFactory:
    def factory(target, work_dir: Path):
        from swarmqa.driver import create_driver

        kind = None if config.driver.kind == "auto" else config.driver.kind
        return create_driver(target, work_dir, kind=kind, video_mode=config.video.mode)

    return factory


def _replay_summary(result: ReplayResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "reproduced": result.reproduced,
        "ran": result.ran,
        "reason": result.reason,
        "finding_id": result.finding_id,
        "kind": result.kind,
        "fingerprint": result.fingerprint,
        "observed_fingerprints": result.observed_fingerprints,
        "final_step_failed": result.final_step_failed,
        "crashed": result.crashed,
        "status": result.result.status if result.result else None,
        "steps": [
            {"index": step.index, "action": step.action, "status": step.status, "message": step.message}
            for step in (result.result.steps if result.result else [])
        ],
    }


def _evidence(campaign_dir: Path, work_dir: Path) -> list[str]:
    found = sorted(
        path for path in work_dir.rglob("*") if path.is_file() and path.suffix.lower() in _EVIDENCE_SUFFIXES
    )
    summary = work_dir / "replay_result.json"
    ordered = [summary] + [path for path in found if path != summary]
    return [public(campaign_dir, path) for path in ordered if path.is_file()]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "device"


def _safe_close(driver) -> None:
    close = getattr(driver, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - already closed or never opened
        return
