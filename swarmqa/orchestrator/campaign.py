"""C8 — schedule shards across workers and merge one campaign report.

See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from swarmqa.backends import create_backend
from swarmqa.backends.local import (
    Executor,
    LocalBackend,
    make_error_result,
    persist_finding,
)
from swarmqa.budgets import CampaignClock
from swarmqa.models import (
    CampaignConfig,
    CampaignResult,
    CampaignStatus,
    Coverage,
    RunOptions,
    Shard,
    SpendSummary,
    WorkerResult,
)
from swarmqa.orchestrator.status import (
    format_campaign_line,
    format_worker_line,
    load_status,
    model_from_plain,
    write_status,
)
from swarmqa.report.dedup import dedup_findings
from swarmqa.report.layout import campaign_dir, ensure_campaign_layout, worker_dir
from swarmqa.report.summary import write_summary
from swarmqa.serialize import dump_json, load_json
from swarmqa.spend import SpendMeter
from swarmqa.util import new_campaign_id

LOCAL_SPEND_NOTE = "max_spend is ignored on the local backend"
_INTERACTIVE = {"scripted", "exploratory", "visual"}
_SCRIPTED_FAIL_KINDS = {"scripted", "suite"}


def run_campaign(
    config: CampaignConfig,
    queue: list[Shard],
    *,
    options: RunOptions | None = None,
    driver_factory: Callable | None = None,
    executor: Executor | None = None,
) -> CampaignResult:
    """Run up to config.workers sessions. Merge artifacts. Survive a worker crash."""
    options = options or RunOptions()
    root, campaign_id, previous = _open_campaign(config, options)
    resume = previous is not None and bool(options.resume_campaign_id)
    to_run, carried_shards = _partition_queue(list(queue), previous, resume)
    carried = _load_carried_results(root, carried_shards, previous)
    rate = _backend_rate(config)
    meter, metered, spend_note = _open_meter(config, rate)
    if resume and previous is not None and not options.reset_spend and previous.spend_estimated:
        meter.add(float(previous.spend_estimated))
    clock = CampaignClock(
        max_wall_time_s=config.budgets.max_wall_time_s,
        max_worker_minutes=config.budgets.max_worker_minutes,
    )
    _warn_gui(config, queue)

    slots = max(1, int(config.workers))
    if config.backend == "local":
        backend: LocalBackend | object = LocalBackend(
            config,
            executor=executor,
            driver_factory=driver_factory,
            campaign_dir=root,
        )
    else:
        backend = create_backend(config, campaign_dir=root)

    status = CampaignStatus(
        campaign_id=campaign_id,
        state="running",
        backend=config.backend,
        workers_configured=slots,
        queue_depth=len(to_run),
        spend_estimated=meter.spent,
        spend_cap=config.spend.max_spend,
        spend_currency=config.spend.currency or "USD",
        pending=[shard.id for shard in to_run],
        report_dir=str(root),
    )
    state_lock = threading.RLock()
    active: dict[str, object] = {}
    pending: list[Shard] = list(to_run)
    results: dict[str, WorkerResult] = dict(carried)
    reserved: dict[str, float] = {}
    worker_seq = _next_worker_index(root)
    stop_reason: str | None = None
    cancelled_workers: set[str] = set()
    started = time.monotonic()

    def publish(*, worker_line: str | None = None) -> None:
        with state_lock:
            status.active_workers = list(active.values())
            status.queue_depth = len(pending)
            status.pending = [shard.id for shard in pending]
            status.spend_estimated = meter.spent
            status.spend_cap = config.spend.max_spend
            status.spend_stop_reason = "spend_cap" if stop_reason == "spend_cap" else None
            status.completed = [item.shard_id for item in results.values() if item.status == "passed"]
            status.failed = [
                item.shard_id for item in results.values() if item.status in {"failed", "error"}
            ]
            status.cancelled = [item.shard_id for item in results.values() if item.status == "cancelled"]
            write_status(status, root)
            campaign_line = format_campaign_line(status)
        print(campaign_line, file=sys.stderr, flush=True)
        if worker_line:
            print(worker_line, file=sys.stderr, flush=True)

    def on_step(worker_id: str, shard_name: str, mode: str, step: str) -> None:
        from swarmqa.models import ActiveWorker

        with state_lock:
            worker = active.get(worker_id)
            if worker is None:
                worker = ActiveWorker(
                    worker_id=worker_id,
                    shard_id="",
                    shard_name=shard_name,
                    mode=mode,
                    step=step,
                )
                active[worker_id] = worker
            worker.shard_name = shard_name
            worker.mode = mode
            worker.step = step
            status.active_workers = list(active.values())
            write_status(status, root)
            line = format_worker_line(worker_id, shard_name, mode, step)
        print(line, file=sys.stderr, flush=True)

    if isinstance(backend, LocalBackend):
        backend.on_step = on_step

    publish()

    def start_shard(pool: ThreadPoolExecutor, shard: Shard) -> None:
        nonlocal worker_seq
        from swarmqa.models import ActiveWorker

        with state_lock:
            worker_id = f"w{worker_seq}"
            worker_seq += 1
            step = _initial_step(shard)
            active[worker_id] = ActiveWorker(
                worker_id=worker_id,
                shard_id=shard.id,
                shard_name=shard.name,
                mode=shard.kind,
                step=step,
            )
        if metered:
            estimate = rate * float(config.cloud.estimated_shard_minutes)
            meter.add(estimate)
            reserved[worker_id] = estimate
        else:
            estimate = 0.0
            reserved[worker_id] = 0.0
        worker_dir(root, worker_id)
        line = format_worker_line(worker_id, shard.name, shard.kind, _initial_step(shard))
        publish(worker_line=line)
        future = pool.submit(backend.run_shard, shard, worker_id)
        inflight[future] = (shard, worker_id, time.monotonic())

    inflight: dict[Future, tuple[Shard, str, float]] = {}
    with ThreadPoolExecutor(max_workers=slots) as pool:
        while pending or inflight:
            if stop_reason is None:
                decision = clock.can_schedule(time.monotonic() - started)
                if not decision.allowed:
                    stop_reason = decision.stop_reason or "budget"
                else:
                    while pending and len(inflight) < slots:
                        decision = clock.can_schedule(time.monotonic() - started)
                        if not decision.allowed:
                            stop_reason = decision.stop_reason or "budget"
                            break
                        if metered:
                            estimate = rate * float(config.cloud.estimated_shard_minutes)
                            spend_decision = meter.can_start(estimate)
                            if not spend_decision.allowed:
                                stop_reason = spend_decision.stop_reason or "spend_cap"
                                break
                        start_shard(pool, pending.pop(0))
            if stop_reason is not None and _overrun_policy(config, stop_reason) == "cancel":
                for _future, (shard, worker_id, _t0) in list(inflight.items()):
                    if worker_id in cancelled_workers:
                        continue
                    cancelled_workers.add(worker_id)
                    try:
                        backend.cancel(worker_id)
                    except Exception as exc:
                        print(
                            f"[campaign {campaign_id}] cancel {worker_id} failed: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        print(
                            format_worker_line(worker_id, shard.name, shard.kind, "cancel"),
                            file=sys.stderr,
                            flush=True,
                        )
            if not inflight:
                break
            done, _still = wait(list(inflight), timeout=0.05, return_when=FIRST_COMPLETED)
            for future in done:
                shard, worker_id, started_at = inflight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = make_error_result(shard, worker_id, str(exc), config.backend)
                result = _normalize_result(result, shard, worker_id, config.backend, started_at)
                if worker_id in cancelled_workers:
                    result.status = "cancelled"
                _settle_spend(meter, metered, rate, config, result, reserved.pop(worker_id, 0.0))
                clock.add_worker_minutes(result.worker_minutes)
                dump_json(result, worker_dir(root, worker_id) / "result.json")
                results[shard.id] = result
                with state_lock:
                    active.pop(worker_id, None)
                publish()

    not_started = [shard for shard in to_run if shard.id not in results]
    ordered = _ordered_results(queue, results)
    merged = _apply_dedup(ordered)
    # Only a complete, clean run may mark missing findings as fixed.
    full_run = (
        stop_reason is None
        and not not_started
        and not resume
        and not options.partial
        and all(item.status in ("passed", "failed") for item in ordered)
    )
    _finalize_findings(merged, root, campaign_id, config, full_run=full_run)
    coverage = Coverage(
        completed=sum(item.status == "passed" for item in ordered),
        failed=sum(item.status in {"failed", "error"} for item in ordered),
        cancelled=sum(item.status == "cancelled" for item in ordered),
        not_started=len(not_started),
        stop_reason=stop_reason,
    )
    spend = SpendSummary(
        max_spend=config.spend.max_spend,
        currency=config.spend.currency or "USD",
        estimated_spent=meter.spent,
        stop_reason="spend_cap" if stop_reason == "spend_cap" else None,
        note=spend_note,
    )
    exit_code = _exit_code(config, ordered)
    result = CampaignResult(
        campaign_id=campaign_id,
        report_dir=str(root),
        results=ordered,
        coverage=coverage,
        spend=spend,
        exit_code=exit_code,
        backend=config.backend,
    )
    write_summary(result, root)
    status.state = "stopped" if stop_reason else "finished"
    status.active_workers = []
    status.queue_depth = len(not_started)
    status.pending = [shard.id for shard in not_started]
    status.completed = [item.shard_id for item in ordered if item.status == "passed"]
    status.failed = [item.shard_id for item in ordered if item.status in {"failed", "error"}]
    status.cancelled = [item.shard_id for item in ordered if item.status == "cancelled"]
    status.spend_estimated = meter.spent
    status.spend_stop_reason = spend.stop_reason
    write_status(status, root)
    print(format_campaign_line(status), file=sys.stderr, flush=True)
    return result


def _open_campaign(config: CampaignConfig, options: RunOptions):
    report_root = Path(config.report_root)
    resume_id = (options.resume_campaign_id or "").strip()
    previous = None
    if resume_id:
        campaign_id = Path(resume_id).name
        root = campaign_dir(report_root, campaign_id)
        if root.is_dir():
            previous = load_status(root)
    else:
        campaign_id = Path(options.campaign_id).name if options.campaign_id else new_campaign_id()
        root = campaign_dir(report_root, campaign_id)
    root = root.resolve()
    ensure_campaign_layout(root)
    return root, campaign_id, previous


def _partition_queue(
    queue: list[Shard],
    previous: CampaignStatus | None,
    resume: bool,
) -> tuple[list[Shard], list[Shard]]:
    if not resume or previous is None:
        return list(queue), []
    rerun = set(previous.failed) | set(previous.pending)
    skip = set(previous.completed) | set(previous.cancelled)
    selected: list[Shard] = []
    carried: list[Shard] = []
    for shard in queue:
        if shard.id in rerun:
            selected.append(shard)
        elif shard.id in skip:
            carried.append(shard)
        else:
            selected.append(shard)
    return selected, carried


def _load_carried_results(
    root: Path,
    carried_shards: list[Shard],
    previous: CampaignStatus | None,
) -> dict[str, WorkerResult]:
    if not carried_shards:
        return {}
    on_disk = _load_worker_results(root)
    found: dict[str, WorkerResult] = {}
    cancelled = set(previous.cancelled) if previous else set()
    for shard in carried_shards:
        existing = on_disk.get(shard.id)
        if existing is not None:
            found[shard.id] = existing
            continue
        status_name = "cancelled" if shard.id in cancelled else "passed"
        found[shard.id] = WorkerResult(
            worker_id="carried",
            shard_id=shard.id,
            shard_name=shard.name,
            shard_kind=shard.kind,
            status=status_name,
            backend=previous.backend if previous else "local",
        )
    return found


def _load_worker_results(root: Path) -> dict[str, WorkerResult]:
    workers = root / "workers"
    if not workers.is_dir():
        return {}
    paths = sorted(workers.glob("*/result.json"), key=lambda path: path.stat().st_mtime)
    found: dict[str, WorkerResult] = {}
    for path in paths:
        try:
            result = model_from_plain(WorkerResult, load_json(path))
        except (OSError, TypeError, ValueError, KeyError):
            continue
        if result.shard_id:
            found[result.shard_id] = result
    return found


def _backend_rate(config: CampaignConfig) -> float:
    if config.backend == "local":
        return 0.0
    try:
        backend = create_backend(config)
        return float(backend.cost_per_worker_minute())
    except Exception:
        if config.backend == "cloud":
            return float(config.cloud.cost_per_worker_minute)
        if config.backend == "vm":
            return float(config.vm.cost_per_worker_minute)
        return 0.0


def _open_meter(config: CampaignConfig, rate: float) -> tuple[SpendMeter, bool, str | None]:
    # Model calls cost money on any backend, so an enabled [llm] meters the
    # campaign even when the machines are free.
    models_cost = _models_cost(config)
    metered = rate > 0 or models_cost
    note = None
    cap = config.spend.max_spend
    currency = config.spend.currency or "USD"
    if config.backend == "local" and cap is not None and not models_cost:
        note = LOCAL_SPEND_NOTE
    if metered and cap is not None and cap > 0:
        meter = SpendMeter(cap, currency)
    else:
        meter = SpendMeter(None, currency)
    return meter, metered, note


def _models_cost(config: CampaignConfig) -> bool:
    return config.llm.enabled and config.explorer.engine == "agent"


def _warn_gui(config: CampaignConfig, queue: list[Shard]) -> None:
    interactive = any(shard.kind in _INTERACTIVE for shard in queue)
    if config.backend != "local" or not interactive:
        return
    if config.app.platform == "ios":
        pool = len(config.app.simulators)
        if config.workers > 1 and pool < config.workers:
            print(
                (
                    f"warning: local backend has {config.workers} iOS workers and "
                    f"{pool} entry(s) in app.simulators. Each worker needs its own "
                    "Simulator. Set app.simulators to one device name or UDID per "
                    "worker, or pin a device with SWARMQA_SIMULATOR_UDID."
                ),
                file=sys.stderr,
                flush=True,
            )
        return
    if config.workers > config.gui_worker_warn_threshold:
        print(
            (
                f"warning: local backend has {config.workers} workers, above the GUI "
                f"threshold of {config.gui_worker_warn_threshold}. Scripted, exploratory, "
                "and visual shards share this display and session. Use the vm or cloud "
                "backend for isolated GUI sessions."
            ),
            file=sys.stderr,
            flush=True,
        )


def _overrun_policy(config: CampaignConfig, stop_reason: str) -> str:
    if stop_reason == "spend_cap":
        return config.spend.overrun
    return config.budgets.on_budget


def _initial_step(shard: Shard) -> str:
    if shard.actions:
        return shard.actions[0].action
    if shard.kind == "suite":
        return "suite"
    if shard.kind == "visual":
        return "compare"
    return "start"


def _normalize_result(
    result: WorkerResult,
    shard: Shard,
    worker_id: str,
    backend: str,
    started_at: float,
) -> WorkerResult:
    result.worker_id = worker_id
    result.shard_id = shard.id
    result.shard_name = shard.name
    result.shard_kind = shard.kind
    result.backend = backend
    if result.worker_minutes <= 0:
        result.worker_minutes = max(0.0, (time.monotonic() - started_at) / 60.0)
    if not result.started_at:
        result.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not result.finished_at:
        result.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return result


def _settle_spend(meter, metered: bool, rate: float, config: CampaignConfig, result: WorkerResult, reserved_cost: float) -> None:
    if not metered:
        result.estimated_cost = result.estimated_cost or 0.0
        return
    actual = result.estimated_cost
    if actual <= 0 and rate > 0:
        actual = reserved_cost if reserved_cost > 0 else rate * float(config.cloud.estimated_shard_minutes)
        result.estimated_cost = actual
    extra = actual - reserved_cost
    if extra > 1e-9:
        meter.add(extra)


def _ordered_results(queue: list[Shard], results: dict[str, WorkerResult]) -> list[WorkerResult]:
    ordered: list[WorkerResult] = []
    seen: set[str] = set()
    for shard in queue:
        item = results.get(shard.id)
        if item is None or shard.id in seen:
            continue
        ordered.append(item)
        seen.add(shard.id)
    return ordered


def _apply_dedup(results: list[WorkerResult]) -> list:
    collected = [finding for item in results for finding in item.findings]
    merged = dedup_findings(collected)
    owners = {finding.fingerprint: finding for finding in merged}
    for item in results:
        kept = []
        seen: set[str] = set()
        for finding in item.findings:
            replacement = owners.get(finding.fingerprint)
            if replacement is None or finding.fingerprint in seen:
                continue
            if replacement.worker_id == item.worker_id:
                kept.append(replacement)
                seen.add(finding.fingerprint)
                owners.pop(finding.fingerprint, None)
        item.findings = kept
    return merged


def _finalize_findings(findings, root: Path, campaign_id: str, config: CampaignConfig, *, full_run: bool) -> None:
    """Enrich findings in place (repro, clip, sources, cross-run state) and
    write findings.json. Evidence is best effort: if the pipeline fails the
    findings are still written as before."""
    source_dir = Path(config.app.source_dir).expanduser() if config.app.source_dir else None
    try:
        from swarmqa.report.pipeline import finalize_findings

        finalize_findings(root, campaign_id, findings, config=config, repo=source_dir, full_run=full_run)
    except Exception as exc:
        print(f"warning: evidence pipeline failed: {exc}", file=sys.stderr)
        _persist_findings(findings, root)


def _persist_findings(findings, root: Path) -> None:
    for finding in findings:
        persist_finding(finding, root)


def _exit_code(config: CampaignConfig, results: list[WorkerResult]) -> int:
    if config.fail_on == "never":
        return 0
    if config.fail_on == "any":
        return 1 if any(item.findings for item in results) else 0
    for item in results:
        if item.status == "error":
            return 1
        if item.shard_kind in _SCRIPTED_FAIL_KINDS and item.status == "failed":
            return 1
    return 0


def _next_worker_index(root: Path) -> int:
    best = 0
    workers = root / "workers"
    if workers.is_dir():
        for path in workers.iterdir():
            name = path.name
            if name.startswith("w") and name[1:].isdigit():
                best = max(best, int(name[1:]))
    return best + 1
