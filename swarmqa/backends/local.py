"""C8 — local worker backend.

See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from swarmqa.errors import ChunkNotReady
from swarmqa.models import (
    CampaignConfig,
    Finding,
    Shard,
    WorkerResult,
)
from swarmqa.orchestrator.status import format_worker_line
from swarmqa.report.layout import ensure_campaign_layout, worker_dir
from swarmqa.serialize import dump_json, load_json

# (shard, worker_id, work_dir, config) -> WorkerResult
Executor = Callable[[Shard, str, Path, CampaignConfig], WorkerResult]
# (worker_id, shard_name, mode, step) -> None
StepCallback = Callable[[str, str, str, str], None]


class LocalBackend:
    name = "local"

    def __init__(
        self,
        config: CampaignConfig,
        *,
        executor: Executor | None = None,
        driver_factory: Callable | None = None,
        campaign_dir: Path | None = None,
        on_step: StepCallback | None = None,
    ):
        self.config = config
        self.executor = executor
        self.driver_factory = driver_factory
        self.campaign_dir = Path(campaign_dir) if campaign_dir is not None else None
        self.on_step = on_step
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._procs: dict[str, subprocess.Popen] = {}

    def cost_per_worker_minute(self) -> float:
        return 0.0

    def run_shard(self, shard: Shard, worker_id: str) -> WorkerResult:
        root = self._root()
        work = worker_dir(root, worker_id)
        if self._is_cancelled(worker_id):
            return make_cancelled_result(shard, worker_id, self.name)
        if self.executor is not None:
            return self.executor(shard, worker_id, work, self.config)
        if self.config.local.isolation == "subprocess":
            return self._run_subprocess(shard, worker_id, root, work)
        return execute_shard(
            shard,
            worker_id,
            work,
            self.config,
            driver_factory=self.driver_factory,
            campaign_root=root,
            on_step=self.on_step,
        )

    def cancel(self, worker_id: str) -> None:
        with self._lock:
            self._cancelled.add(worker_id)
            proc = self._procs.get(worker_id)
        terminate_live_proc(worker_id)
        if proc is not None and proc.poll() is None:
            _terminate_process_group(proc)

    def _is_cancelled(self, worker_id: str) -> bool:
        with self._lock:
            return worker_id in self._cancelled

    def _root(self) -> Path:
        if self.campaign_dir is not None:
            root = self.campaign_dir
        else:
            root = Path(self.config.report_root) / "local-adhoc"
        ensure_campaign_layout(root)
        return root

    def _run_subprocess(
        self,
        shard: Shard,
        worker_id: str,
        root: Path,
        work: Path,
    ) -> WorkerResult:
        shard_path = work / "shard.json"
        config_path = work / "config.json"
        dump_json(shard, shard_path)
        dump_json(self.config, config_path)
        env = os.environ.copy()
        repo_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
        command = [
            sys.executable,
            "-m",
            "swarmqa.worker",
            "--campaign-dir",
            str(root),
            "--shard-file",
            str(shard_path),
            "--config-json",
            str(config_path),
            "--worker-id",
            worker_id,
        ]
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=os.getcwd(),
        )
        with self._lock:
            self._procs[worker_id] = proc
            already_cancelled = worker_id in self._cancelled
        if already_cancelled:
            _terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            with self._lock:
                self._procs.pop(worker_id, None)
        log_path = _raw_dir(root) / f"{worker_id}-{_safe_shard_id(shard.id)}.worker.log"
        log_path.write_text(
            f"exit={proc.returncode}\n{stdout or ''}{stderr or ''}",
            encoding="utf-8",
        )
        result_path = work / "result.json"
        if result_path.is_file():
            from swarmqa.orchestrator.status import model_from_plain

            try:
                return model_from_plain(WorkerResult, load_json(result_path))
            except (TypeError, ValueError, KeyError) as exc:
                return make_error_result(shard, worker_id, f"invalid worker result: {exc}", self.name)
        message = stderr.strip() or stdout.strip() or "worker did not write result.json"
        if self._is_cancelled(worker_id):
            result = make_cancelled_result(shard, worker_id, self.name)
            result.error = message
            return result
        return make_error_result(shard, worker_id, message, self.name)


def execute_shard(
    shard: Shard,
    worker_id: str,
    work_dir: Path,
    config: CampaignConfig,
    *,
    driver_factory: Callable | None = None,
    campaign_root: Path | None = None,
    on_step: StepCallback | None = None,
) -> WorkerResult:
    """Run one shard in-process. ChunkNotReady from explorers becomes status error."""
    root = campaign_root or _infer_campaign_root(work_dir)
    ensure_campaign_layout(root)
    work_dir.mkdir(parents=True, exist_ok=True)
    started = _now()
    try:
        if shard.kind == "scripted":
            result = _run_scripted(shard, worker_id, work_dir, config, driver_factory, on_step)
        elif shard.kind == "exploratory":
            result = _run_exploratory(shard, worker_id, work_dir, config, driver_factory, on_step)
        elif shard.kind == "suite":
            result = _run_suite(shard, worker_id, work_dir, config, root, on_step)
        elif shard.kind == "visual":
            result = _run_visual(shard, worker_id, work_dir, config, driver_factory, on_step)
        else:
            result = make_error_result(
                shard, worker_id, f"unknown shard kind: {shard.kind}", config.backend
            )
    except ChunkNotReady as exc:
        result = make_error_result(shard, worker_id, str(exc), config.backend)
    except Exception as exc:
        result = make_error_result(shard, worker_id, str(exc), config.backend)
    if not result.started_at:
        result.started_at = started
    if not result.finished_at:
        result.finished_at = _now()
    result.worker_id = result.worker_id or worker_id
    result.shard_id = result.shard_id or shard.id
    result.shard_name = result.shard_name or shard.name
    result.shard_kind = result.shard_kind or shard.kind
    result.backend = result.backend or config.backend
    return result


def make_error_result(
    shard: Shard,
    worker_id: str,
    message: str,
    backend: str = "local",
) -> WorkerResult:
    now = _now()
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        shard_name=shard.name,
        shard_kind=shard.kind,
        status="error",
        error=message,
        started_at=now,
        finished_at=now,
        backend=backend,
    )


def make_cancelled_result(shard: Shard, worker_id: str, backend: str = "local") -> WorkerResult:
    now = _now()
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        shard_name=shard.name,
        shard_kind=shard.kind,
        status="cancelled",
        error="cancelled",
        started_at=now,
        finished_at=now,
        backend=backend,
    )


def _run_scripted(shard, worker_id, work_dir, config, driver_factory, on_step) -> WorkerResult:
    _emit(on_step, worker_id, shard, "launch")
    driver = _make_driver(config, work_dir, driver_factory)
    try:
        from swarmqa.explorer.scripted import run_scripted

        step = shard.actions[0].action if shard.actions else "scripted"
        _emit(on_step, worker_id, shard, step)
        return run_scripted(shard, driver, config, worker_id=worker_id, work_dir=work_dir)
    finally:
        _close_driver(driver)


def _run_exploratory(shard, worker_id, work_dir, config, driver_factory, on_step) -> WorkerResult:
    _emit(on_step, worker_id, shard, "explore")
    driver = _make_driver(config, work_dir, driver_factory)
    try:
        from swarmqa.explorer.exploratory import run_exploratory

        return run_exploratory(shard, driver, config, worker_id=worker_id, work_dir=work_dir)
    finally:
        _close_driver(driver)


def _run_suite(shard, worker_id, work_dir, config, root: Path, on_step) -> WorkerResult:
    command = shard.suite_command or config.suite.command
    _emit(on_step, worker_id, shard, "suite")
    started = _now()
    if not command:
        return make_error_result(shard, worker_id, "suite command is empty", config.backend)
    log_path = _raw_dir(root) / f"{worker_id}-{_safe_shard_id(shard.id)}.log"
    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=work_dir,
    )
    register_live_proc(worker_id, proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        clear_live_proc(worker_id, proc)
    log_path.write_text(
        f"$ {command}\nexit={proc.returncode}\n{stdout or ''}{stderr or ''}",
        encoding="utf-8",
    )
    status = "passed" if proc.returncode == 0 else "failed"
    findings: list[Finding] = []
    if status == "failed":
        findings.append(_suite_finding(shard, worker_id, command, log_path, config.backend))
        persist_finding(findings[-1], root)
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        shard_name=shard.name,
        shard_kind=shard.kind,
        status=status,
        findings=findings,
        started_at=started,
        finished_at=_now(),
        backend=config.backend,
        error=None if status == "passed" else f"suite exit {proc.returncode}",
    )


def _run_visual(shard, worker_id, work_dir, config, driver_factory, on_step) -> WorkerResult:
    from swarmqa.visual.diff import compare_screenshot
    from swarmqa.visual.judge import apply_judgment

    names = list(shard.visual_names)
    baseline_dir = Path(config.visual.baseline_dir)
    if not baseline_dir.is_absolute():
        baseline_dir = Path.cwd() / baseline_dir
    if not names and baseline_dir.is_dir():
        names = sorted(path.stem for path in baseline_dir.glob("*.png"))
    started = _now()
    if not names:
        return WorkerResult(
            worker_id=worker_id,
            shard_id=shard.id,
            shard_name=shard.name,
            shard_kind=shard.kind,
            status="passed",
            started_at=started,
            finished_at=_now(),
            backend=config.backend,
        )
    _emit(on_step, worker_id, shard, "compare")
    driver = _make_driver(config, work_dir, driver_factory)
    findings: list[Finding] = []
    judge_error: str | None = None
    try:
        driver.launch()
        for name in names:
            _emit(on_step, worker_id, shard, name)
            current = driver.screenshot(name)
            if config.visual.judgment.enabled:
                judged, judge_error = apply_judgment(
                    current,
                    config,
                    worker_id=worker_id,
                    shard_id=shard.id,
                    backend=config.backend,
                    error=judge_error,
                    name=name,
                )
                if judged is not None:
                    findings.append(judged)
                    persist_finding(judged, _infer_campaign_root(work_dir))
            baseline = baseline_dir / f"{name}.png"
            diff_out = work_dir / f"{name}.diff.png"
            diff = compare_screenshot(
                current,
                baseline,
                diff_out,
                threshold=config.visual.threshold,
            )
            if not diff.passed:
                finding = _visual_finding(shard, worker_id, name, diff, config.backend)
                findings.append(finding)
                persist_finding(finding, _infer_campaign_root(work_dir))
    finally:
        _close_driver(driver)
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        shard_name=shard.name,
        shard_kind=shard.kind,
        status="failed" if findings else "passed",
        findings=findings,
        started_at=started,
        finished_at=_now(),
        backend=config.backend,
        error=judge_error,
    )


def _make_driver(config: CampaignConfig, work_dir: Path, driver_factory: Callable | None):
    factory = driver_factory or _default_driver_factory(config)
    return factory(config.app, work_dir)


def _default_driver_factory(config: CampaignConfig) -> Callable:
    def factory(target, work_dir):
        from swarmqa.driver import create_driver

        kind = None if config.driver.kind == "auto" else config.driver.kind
        return create_driver(target, work_dir, kind=kind, video_mode=config.video.mode)

    return factory


def _close_driver(driver) -> None:
    close = getattr(driver, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return


def _emit(on_step: StepCallback | None, worker_id: str, shard: Shard, step: str) -> None:
    if on_step is not None:
        on_step(worker_id, shard.name, shard.kind, step)
        return
    print(format_worker_line(worker_id, shard.name, shard.kind, step), file=sys.stderr, flush=True)


def _suite_finding(shard: Shard, worker_id: str, command: str, log_path: Path, backend: str) -> Finding:
    title = f"Suite command failed: {shard.name}"
    return Finding(
        id=f"f-{worker_id}-{shard.id}",
        title=title,
        severity="high",
        kind="suite_failure",
        steps=[command, f"log: {log_path}"],
        fingerprint=_fingerprint("suite_failure", title, command),
        worker_id=worker_id,
        backend=backend,
        shard_id=shard.id,
        details=f"suite exit non-zero; log {log_path}",
    )


def _visual_finding(shard: Shard, worker_id: str, name: str, diff, backend: str) -> Finding:
    title = f"Visual diff failed: {name}"
    shots = []
    if getattr(diff, "diff_path", None):
        shots.append(str(diff.diff_path))
    return Finding(
        id=f"f-{worker_id}-{shard.id}-{name}",
        title=title,
        severity="medium",
        kind="visual",
        steps=[f"compare {name}", diff.message or ""],
        fingerprint=_fingerprint("visual", title, name),
        worker_id=worker_id,
        backend=backend,
        shard_id=shard.id,
        screenshots=shots,
        details=diff.message or "",
    )


def _fingerprint(kind: str, title: str, target: str = "") -> str:
    try:
        from swarmqa.reporter.findings import fingerprint_for

        return fingerprint_for(kind, title, target)
    except ChunkNotReady:
        raw = kind + "\n" + title.strip().lower() + "\n" + target.strip().lower()
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def persist_finding(finding: Finding, campaign_root: Path) -> None:
    try:
        from swarmqa.reporter.findings import write_finding

        write_finding(finding, campaign_root)
    except ChunkNotReady:
        path = campaign_root / "findings" / f"{finding.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {finding.title}",
            "",
            f"Severity: {finding.severity}",
            f"Kind: {finding.kind}",
            f"Worker: {finding.worker_id}",
            f"Backend: {finding.backend}",
            "",
            "## Steps",
            "",
            *[f"- {step}" for step in finding.steps],
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")


def _raw_dir(root: Path) -> Path:
    path = root / "raw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _infer_campaign_root(work_dir: Path) -> Path:
    if work_dir.parent.name == "workers":
        return work_dir.parent.parent
    return work_dir


def _safe_shard_id(shard_id: str) -> str:
    return shard_id.replace("/", "-").replace(os.sep, "-")


_LIVE_PROCS: dict[str, subprocess.Popen] = {}
_LIVE_LOCK = threading.Lock()


def register_live_proc(worker_id: str, proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        _LIVE_PROCS[worker_id] = proc


def clear_live_proc(worker_id: str, proc: subprocess.Popen | None = None) -> None:
    with _LIVE_LOCK:
        current = _LIVE_PROCS.get(worker_id)
        if proc is None or current is proc:
            _LIVE_PROCS.pop(worker_id, None)


def terminate_live_proc(worker_id: str) -> None:
    with _LIVE_LOCK:
        proc = _LIVE_PROCS.get(worker_id)
    if proc is not None and proc.poll() is None:
        _terminate_process_group(proc)


def terminate_all_live_procs() -> None:
    with _LIVE_LOCK:
        procs = list(_LIVE_PROCS.values())
    for proc in procs:
        if proc.poll() is None:
            _terminate_process_group(proc)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
