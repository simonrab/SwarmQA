"""C9a — Tart VM backend.

See docs/CONTRACTS.md section C9 and docs/backends.md.

`runner(args, **kwargs)` is the only way this class talks to Tart. Tests pass
a fake. One `run_shard` call owns one VM named `aqa-<worker-id>` and never
stops a sibling.
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

from swarmqa.errors import BackendUnavailable
from swarmqa.models import CampaignConfig, Finding, Shard, WorkerResult
from swarmqa.report.layout import ensure_campaign_layout
from swarmqa.serialize import dump_json, load_json

# macOS guests mount Tart --dir shares under this folder. The mount name
# SwarmQA uses is "swarmqa", so the guest sees GUEST_SHARE.
GUEST_SHARE = "/Volumes/My Shared Files/swarmqa"

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
    return subprocess.run(args, capture_output=True, text=True, check=False)


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

    def __init__(self, config: CampaignConfig, runner: Runner | None = None):
        self.config = config
        self.runner = runner if runner is not None else _default_runner
        self._runner_injected = runner is not None

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
        root = Path(campaign_dir) if campaign_dir is not None else Path(self.config.report_root) / "vm"
        state = {"started": False, "created": False}
        try:
            result = self._execute(shard, worker_id, vm_name, root, state)
        except Exception as exc:
            result = _error_result(shard, worker_id, exc)
        shutdown_error = self._shutdown(vm_name, state)
        if shutdown_error and result.status == "passed":
            result.status = "error"
            result.error = shutdown_error
        return result

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
        )
        app_path = self.config.app.path
        guest_config = _guest_config(self.config)
        if app_path and Path(app_path).is_dir():
            bundle = Path(app_path)
            shutil.copytree(bundle, share / "app" / bundle.name)
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
            return WorkerResult(
                worker_id=worker_id,
                shard_id=shard.id,
                status="passed",
                started_at=started_at,
                backend="vm",
                shard_name=shard.name,
                shard_kind=shard.kind,
            )
        try:
            payload = load_json(path)
        except json.JSONDecodeError as exc:
            return _error_result(shard, worker_id, exc, started_at=started_at)
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
            errors.extend(self._best_effort([self._tart_bin(), "stop", vm_name]))
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
                findings.append(Finding(**item))
            except TypeError:
                continue
    return findings


def _merge_tree(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = dest / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
