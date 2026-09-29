"""C9a — Tart VM backend.

See docs/CONTRACTS.md section C9, docs/backends.md and docs/devices.md.

`runner(args, **kwargs)` is the only way this class talks to Tart. Tests pass
a fake. One `run_shard` call owns one VM named `aqa-<worker-id>` and never
stops a sibling. Every running VM holds one of the host-wide Tart slots
(at most two, Apple's macOS VM licence limit), shared with `TartVMPool`.
"""

from __future__ import annotations

import copy
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from swarmqa.devices.capacity import HostResources, host_resources, vm_capacity
from swarmqa.devices.commands import run as run_command
from swarmqa.devices.locks import LockTimeout, acquire_slot
from swarmqa.errors import BackendUnavailable
from swarmqa.models import CampaignConfig, Finding, Shard, WorkerResult
from swarmqa.report.layout import ensure_campaign_layout
from swarmqa.serialize import dump_json, load_json

# macOS guests mount Tart --dir shares under this folder. The mount name
# SwarmQA uses is "swarmqa", so the guest sees GUEST_SHARE.
GUEST_SHARE = "/Volumes/My Shared Files/swarmqa"

# Host-wide lock prefix for running Tart VMs. TartVMPool uses the same slots.
TART_SLOT_PREFIX = "tart-vm-slot"

_LOCAL_HINT = "Set backend = local. See docs/backends.md."
_ARM_MACHINES = {"arm64", "aarch64", "arm64e"}
Runner = Callable[..., Any]


@dataclass
class _Command:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class _RunnerError(RuntimeError):
    """A Tart command failed for this shard only."""

    def __init__(self, args: list[str], returncode: int, stderr: str):
        self.command = args
        self.returncode = returncode
        joined = " ".join(args)
        detail = stderr.strip()
        message = f"{joined} failed with exit {returncode}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _policy_cancels(config: CampaignConfig) -> bool:
    return config.spend.overrun == "cancel" or config.budgets.on_budget == "cancel"


def _default_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run Tart on the host. `background=True` starts `tart run` and returns."""
    if kwargs.get("background"):
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return subprocess.CompletedProcess(args, 0, "", "")
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=kwargs.get("timeout"))


class VMNotReady(RuntimeError):
    """A started VM did not answer `tart ip` and `tart exec <vm> true` in time."""


def wait_for_vm(
    runner: Runner,
    tart_bin: str,
    vm_name: str,
    *,
    timeout_s: float,
    poll_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll `tart ip <vm>`, then `tart exec <vm> true`, until both succeed.

    Returns the guest IP. Raises `VMNotReady` once `timeout_s` has passed.
    """
    deadline = clock() + max(0.0, timeout_s)
    ip = ""
    last = "no answer yet"
    while True:
        if not ip:
            found = run_command(runner, [tart_bin, "ip", vm_name], timeout=30)
            lines = found.stdout.strip().splitlines()
            if found.ok and lines:
                ip = lines[-1].strip()
            else:
                last = f"tart ip: {found.detail()}"
        if ip:
            probe = run_command(runner, [tart_bin, "exec", vm_name, "true"], timeout=30)
            if probe.ok:
                return ip
            last = f"tart exec {vm_name} true: {probe.detail()}"
        remaining = deadline - clock()
        if remaining <= 0:
            raise VMNotReady(
                f"VM {vm_name} was not ready after {timeout_s:g}s ({last}). "
                "Check that the image runs the Tart guest agent."
            )
        sleep(min(poll_s, remaining))


def _vm_names(stdout: str) -> set[str]:
    """Parse `tart list` JSON or the default table."""
    text = (stdout or "").strip()
    if not text:
        return set()
    if text[0] in "[{":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        names = _names_from_json(payload)
        if names is not None:
            return names
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        header = parts[0].lower()
        if header in {"source", "name"}:
            continue
        if header in {"local", "oci", "remote"} and len(parts) >= 2:
            names.add(parts[1])
        else:
            names.add(parts[0])
    return names


def _names_from_json(payload: Any) -> set[str] | None:
    if not isinstance(payload, list):
        return None
    names: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = item.get("Name", item.get("name"))
            if name:
                names.add(str(name))
    return names


def _apple_silicon() -> bool:
    if sys.platform != "darwin":
        return False
    return platform.machine().lower() in _ARM_MACHINES


class TartBackend:
    name = "vm"

    def __init__(
        self,
        config: CampaignConfig,
        runner: Runner | None = None,
        *,
        campaign_dir: str | Path | None = None,
        lock_root: str | Path | None = None,
        max_vms: int | None = None,
        resources: HostResources | None = None,
        boot_timeout_s: float = 300.0,
        slot_timeout_s: float = 1800.0,
        poll_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.runner = runner if runner is not None else _default_runner
        self._runner_injected = runner is not None
        self.campaign_dir = Path(campaign_dir) if campaign_dir is not None else None
        self.lock_root = lock_root
        self.boot_timeout_s = boot_timeout_s
        self.slot_timeout_s = slot_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self._max_vms = max_vms
        self._resources = resources
        # Slots whose VM would not stop, held for this process's life so the
        # two-VM cap still counts the VM that may be running.
        self._stuck_slots: list[Any] = []

    def vm_capacity(self) -> int:
        """Tart VMs this host may run at once (never more than two)."""
        resources = self._resources or host_resources()
        count = vm_capacity(resources)
        if self._max_vms is not None:
            count = min(count, max(0, self._max_vms))
        return count

    def cost_per_worker_minute(self) -> float:
        return float(self.config.vm.cost_per_worker_minute)

    def ensure_available(self) -> None:
        """Raise BackendUnavailable unless this host can run Tart."""
        if not _apple_silicon():
            raise BackendUnavailable(
                "Tart VMs require Apple Silicon macOS (arm64). " + _LOCAL_HINT
            )
        binary = self._tart_bin()
        if not self._binary_available(binary):
            raise BackendUnavailable(
                f"The tart binary {binary!r} was not found. "
                "Install Tart using the steps in docs/backends.md. "
                + _LOCAL_HINT
            )

    def run_shard(
        self,
        shard: Shard,
        worker_id: str,
        *,
        campaign_dir: str | Path | None = None,
    ) -> WorkerResult:
        """Clone or reuse `aqa-<worker-id>`, run the worker, pull artifacts.

        A runner failure returns `WorkerResult` with status `error` for this
        shard. Other VMs are left alone.
        """
        if not self._runner_injected:
            self.ensure_available()
        vm_name = f"aqa-{worker_id}"
        root = self._campaign_root(campaign_dir, worker_id)
        slots = self.vm_capacity()
        try:
            _index, slot = acquire_slot(
                TART_SLOT_PREFIX,
                slots,
                self.slot_timeout_s,
                root=self.lock_root,
                poll_s=self.poll_s,
                sleep=self._sleep,
                clock=self._clock,
            )
        except LockTimeout:
            return _error_result(
                shard,
                worker_id,
                BackendUnavailable(
                    f"no Tart VM slot free after {self.slot_timeout_s:g}s: this host runs at "
                    f"most {slots} macOS VMs at once (Apple's licence allows two)"
                ),
            )
        state = {"started": False, "created": False, "stop_failed": False}
        shutdown_error = None
        try:
            try:
                result = self._execute(shard, worker_id, vm_name, root, state)
            except Exception as exc:
                result = _error_result(shard, worker_id, exc)
            shutdown_error = self._shutdown(vm_name, state)
        finally:
            if state["stop_failed"]:
                self._stuck_slots.append(slot)
            else:
                slot.release()
        if shutdown_error and result.status == "passed":
            result.status = "error"
            result.error = shutdown_error
        return result

    def _campaign_root(self, explicit: str | Path | None, worker_id: str) -> Path:
        """The campaign directory results and artifacts belong in.

        An explicit argument wins, then the constructor's `campaign_dir`. The
        orchestrator passes neither for `backend = vm`, but it creates
        `<campaign>/workers/<worker-id>` before `run_shard`, so the running
        campaign that has that directory is the one. `report_root/vm` is the
        last resort for ad hoc calls.
        """
        if explicit is not None:
            return Path(explicit)
        if self.campaign_dir is not None:
            return self.campaign_dir
        report_root = Path(self.config.report_root)
        found = _running_campaign_for(report_root, worker_id)
        return found if found is not None else report_root / "vm"

    def cancel(self, worker_id: str) -> None:
        """Stop `aqa-<worker-id>` when overrun policy is cancel.

        Drain leaves the guest running. The orchestrator calls this for the
        worker that should stop; siblings are not touched.
        """
        if not _policy_cancels(self.config):
            return
        self._invoke([self._tart_bin(), "stop", f"aqa-{worker_id}"])

    def _execute(
        self,
        shard: Shard,
        worker_id: str,
        vm_name: str,
        campaign: Path,
        state: dict[str, bool],
    ) -> WorkerResult:
        started_at = _now()
        timer = _Timer()
        ensure_campaign_layout(campaign)
        share = self._prepare_share(shard, worker_id, campaign)
        existing = self._listed_vms()
        if vm_name not in existing:
            image = self.config.vm.image
            if not image:
                raise _RunnerError(
                    [self._tart_bin(), "clone", vm_name],
                    1,
                    f"vm.image is not set; cannot clone {vm_name}",
                )
            self._invoke([self._tart_bin(), "clone", image, vm_name])
            state["created"] = True
        self._invoke(
            [self._tart_bin(), "run", f"--dir=swarmqa:{share}", vm_name],
            background=True,
            host_share=str(share),
            worker_id=worker_id,
            campaign_dir=str(campaign),
        )
        state["started"] = True
        wait_for_vm(
            self.runner,
            self._tart_bin(),
            vm_name,
            timeout_s=self.boot_timeout_s,
            poll_s=self.poll_s,
            sleep=self._sleep,
            clock=self._clock,
        )
        self._invoke(
            self._worker_command(vm_name, worker_id),
            host_share=str(share),
            worker_id=worker_id,
            campaign_dir=str(campaign),
        )
        self._pull_artifacts(share, campaign)
        result = self._result_from_disk(shard, worker_id, campaign, started_at)
        elapsed = timer.minutes()
        result.worker_minutes = elapsed
        result.estimated_cost = self.cost_per_worker_minute() * elapsed
        result.backend = "vm"
        result.finished_at = result.finished_at or _now()
        return result

    def _prepare_share(self, shard: Shard, worker_id: str, campaign: Path) -> Path:
        share = campaign / "raw" / "tart" / worker_id
        if share.exists():
            shutil.rmtree(share)
        inbound = share / "worker_entry"
        inbound.mkdir(parents=True, exist_ok=True)
        (share / "workers").mkdir()
        (share / "media").mkdir()
        package = Path(__file__).resolve().parents[1]
        shutil.copytree(
            package,
            inbound / "swarmqa",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            symlinks=True,
        )
        app_path = self.config.app.path
        guest_config = _guest_config(self.config)
        if app_path and Path(app_path).is_dir():
            bundle = Path(app_path)
            # .app bundles (macOS frameworks especially) are full of symlinks.
            shutil.copytree(bundle, share / "app" / bundle.name, symlinks=True)
            guest_config.app.path = f"{GUEST_SHARE}/app/{bundle.name}"
        dump_json(shard, share / "shard.json")
        dump_json(guest_config, share / "config.json")
        return share

    def _worker_command(self, vm_name: str, worker_id: str) -> list[str]:
        guest = GUEST_SHARE
        return [
            self._tart_bin(),
            "exec",
            vm_name,
            "--",
            "env",
            f"PYTHONPATH={guest}/worker_entry",
            "python",
            "-m",
            "swarmqa.worker",
            "--campaign-dir",
            guest,
            "--shard-file",
            f"{guest}/shard.json",
            "--config-json",
            f"{guest}/config.json",
            "--worker-id",
            worker_id,
        ]

    def _listed_vms(self) -> set[str]:
        completed = self._invoke([self._tart_bin(), "list"])
        return _vm_names(completed.stdout)

    def _pull_artifacts(self, share: Path, campaign: Path) -> None:
        for name in ("workers", "media"):
            source = share / name
            if source.exists():
                _merge_tree(source, campaign / name)

    def _result_from_disk(
        self,
        shard: Shard,
        worker_id: str,
        campaign: Path,
        started_at: str,
    ) -> WorkerResult:
        path = campaign / "workers" / worker_id / "result.json"
        if not path.is_file():
            # A guest that exits without a result did not pass.
            return _error_result(
                shard,
                worker_id,
                _RunnerError(["result.json"], 1, f"the guest worker wrote no result at {path}"),
                started_at=started_at,
            )
        try:
            payload = load_json(path)
        except (OSError, ValueError) as exc:
            return _error_result(
                shard,
                worker_id,
                _RunnerError(["result.json"], 1, f"could not read {path}: {exc}"),
                started_at=started_at,
            )
        if not isinstance(payload, dict):
            return _error_result(
                shard,
                worker_id,
                _RunnerError(["result.json"], 1, "worker result was not an object"),
                started_at=started_at,
            )
        return _result_from_payload(payload, shard, worker_id, started_at)

    def _shutdown(self, vm_name: str, state: dict[str, bool]) -> str | None:
        if not state["started"] and not state["created"]:
            return None
        errors: list[str] = []
        if state["started"]:
            stop_errors = self._best_effort([self._tart_bin(), "stop", vm_name])
            state["stop_failed"] = bool(stop_errors)
            errors.extend(stop_errors)
        if not self.config.vm.recycle:
            errors.extend(self._best_effort([self._tart_bin(), "delete", vm_name]))
        return "; ".join(errors) or None

    def _best_effort(self, args: list[str]) -> list[str]:
        try:
            self._invoke(args)
        except Exception as exc:
            return [str(exc)]
        return []

    def _invoke(self, args: list[str], **kwargs: Any) -> _Command:
        try:
            raw = self.runner(args, **kwargs)
        except AssertionError:
            raise
        except Exception as exc:
            raise _RunnerError(args, 1, str(exc)) from exc
        code, stdout, stderr = _normalize(raw)
        if code != 0:
            raise _RunnerError(args, code, stderr)
        return _Command(args, code, stdout, stderr)

    def _tart_bin(self) -> str:
        return self.config.vm.tart_bin or "tart"

    def _binary_available(self, binary: str) -> bool:
        path = Path(binary)
        if path.is_absolute():
            return path.is_file() and os.access(path, os.X_OK)
        return shutil.which(binary) is not None


class _Timer:
    def __init__(self) -> None:
        self._started = time.perf_counter()

    def minutes(self) -> float:
        return max(0.0, (time.perf_counter() - self._started) / 60.0)


def _guest_config(config: CampaignConfig) -> CampaignConfig:
    return copy.deepcopy(config)


def _normalize(raw: Any) -> tuple[int, str, str]:
    if raw is None:
        return 0, "", ""
    code = int(getattr(raw, "returncode", 0) or 0)
    stdout = getattr(raw, "stdout", "") or ""
    stderr = getattr(raw, "stderr", "") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return code, str(stdout), str(stderr)


def _error_result(
    shard: Shard,
    worker_id: str,
    exc: BaseException,
    *,
    started_at: str = "",
) -> WorkerResult:
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        status="error",
        error=str(exc),
        started_at=started_at or _now(),
        finished_at=_now(),
        backend="vm",
        shard_name=shard.name,
        shard_kind=shard.kind,
    )


def _result_from_payload(
    payload: dict[str, Any],
    shard: Shard,
    worker_id: str,
    started_at: str,
) -> WorkerResult:
    status = str(payload.get("status") or "passed")
    return WorkerResult(
        worker_id=str(payload.get("worker_id") or worker_id),
        shard_id=str(payload.get("shard_id") or shard.id),
        status=status,  # type: ignore[arg-type]
        findings=_findings(payload.get("findings")),
        started_at=str(payload.get("started_at") or started_at),
        finished_at=str(payload.get("finished_at") or ""),
        error=payload.get("error"),
        estimated_cost=float(payload.get("estimated_cost") or 0),
        worker_minutes=float(payload.get("worker_minutes") or 0),
        backend="vm",
        shard_name=str(payload.get("shard_name") or shard.name),
        shard_kind=str(payload.get("shard_kind") or shard.kind),
    )


def _findings(raw: Any) -> list[Finding]:
    if not isinstance(raw, list):
        return []
    findings: list[Finding] = []
    for item in raw:
        if isinstance(item, Finding):
            findings.append(item)
        elif isinstance(item, dict):
            try:
                findings.append(Finding.from_dict(item))
            except TypeError:
                continue
    return findings


def _merge_tree(source: Path, dest: Path) -> None:
    """Copy `source` over `dest`, keeping symlinks as symlinks."""
    shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)


def _running_campaign_for(report_root: Path, worker_id: str) -> Path | None:
    """Newest running campaign under `report_root` that has `workers/<worker_id>`."""
    if not report_root.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for campaign in report_root.iterdir():
        worker = campaign / "workers" / worker_id
        if not worker.is_dir():
            continue
        try:
            status = load_json(campaign / "status.json")
        except (OSError, ValueError):
            continue
        if not isinstance(status, dict) or status.get("state") != "running":
            continue
        stamp = worker.stat().st_mtime
        if best is None or stamp > best[0]:
            best = (stamp, campaign)
    return best[1] if best else None
