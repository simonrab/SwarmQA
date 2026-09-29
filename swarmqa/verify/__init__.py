"""verify_fix: rebuild and replay a finding to confirm a fix.

Contract (signatures are fixed; WP-C2 implements the bodies, WP-C1's MCP
tools call them):

- `run_verify(...)` does the work in-process and returns a finished
  `VerifyResult` (state `passed`, `failed` or `error`).
- `start_verify(...)` launches `run_verify` in a detached process and returns
  at once with state `running` and a `verify_id`.
- `verify_status(verify_id)` reads the result the detached run writes.

`passed` means the finding did not reproduce on any device. Each verify run
writes to `<report_root>/<campaign_id>/verify/<verify_id>/` (a `status.json`
plus replay artifacts per device). See docs/verify.md.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from swarmqa.mcp.tools import VerifyResult
from swarmqa.models import CampaignConfig


def run_verify(
    finding_id: str,
    *,
    config: CampaignConfig,
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
    verify_id: str | None = None,
    driver_factory: Callable[[Any, Path], Any] | None = None,
) -> VerifyResult:
    """Build (unless `build` is False), replay the finding on `devices` devices, and return the verdict."""
    from swarmqa.verify.core import verify

    return verify(
        finding_id,
        config=config,
        campaign_id=campaign_id,
        devices=devices,
        build=build,
        verify_id=verify_id,
        driver_factory=driver_factory,
    ).result


def start_verify(
    finding_id: str,
    *,
    config_path: str | Path = "aqa.config.toml",
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
) -> VerifyResult:
    """Start `run_verify` in a detached process; return state `running` with its `verify_id`."""
    from swarmqa.config import load_config
    from swarmqa.errors import ConfigError
    from swarmqa.verify import status as status_mod
    from swarmqa.verify.core import VerifySetupError, prepare

    verify_id = status_mod.new_verify_id()
    config_file = Path(config_path).expanduser().resolve()
    try:
        config = load_config(config_file)
    except ConfigError as exc:
        return VerifyResult(verify_id, finding_id, "error", devices=devices, message=str(exc))
    if devices < 1:
        return VerifyResult(verify_id, finding_id, "error", devices=devices, message="devices must be at least 1")
    try:
        prepared = prepare(config, finding_id, campaign_id)
    except VerifySetupError as exc:
        return VerifyResult(verify_id, finding_id, "error", devices=devices, message=str(exc))

    directory = status_mod.verify_dir(prepared.campaign_dir, verify_id)
    directory.mkdir(parents=True, exist_ok=True)
    result = VerifyResult(verify_id, finding_id, "running", devices=devices)
    status_mod.write_status(directory, result, campaign_id=prepared.campaign_dir.name, phase="starting")
    status_mod.record_index(verify_id, directory)
    command = [
        sys.executable, "-m", "swarmqa.verify._runner", finding_id,
        "--config", str(config_file),
        "--campaign", prepared.campaign_dir.name,
        "--devices", str(devices),
        "--verify-id", verify_id,
        "--status-dir", str(directory),
    ]
    if not build:
        command.append("--no-build")
    log = (directory / status_mod.RUNNER_LOG).open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        result.state = "error"
        result.message = f"could not start the verify runner: {exc}"
        status_mod.write_status(directory, result, phase="done")
        return result
    finally:
        log.close()
    with _LAUNCHED_LOCK:
        _LAUNCHED[verify_id] = process
    # A separate file, so it can't race the runner's own status.json writes.
    (directory / status_mod.RUNNER_PID).write_text(f"{process.pid}\n", encoding="utf-8")
    return result


def verify_status(verify_id: str, *, report_root: str | Path | None = None) -> VerifyResult:
    """Current state of a verify run started with `start_verify`."""
    from swarmqa.verify import build as build_mod
    from swarmqa.verify import status as status_mod

    _reap()
    directory = status_mod.find_verify_dir(verify_id, report_root)
    if directory is None:
        return VerifyResult(verify_id, "", "error", message=f"verify run {verify_id} not found")
    document = status_mod.read_document(directory / status_mod.STATUS_JSON)
    if document is None:
        return VerifyResult(verify_id, "", "error", message=f"could not read {directory / status_mod.STATUS_JSON}")
    result = status_mod.result_from_document(document)
    if result.state != "running":
        return result
    pid = document.get("pid")
    if not isinstance(pid, int):
        pid = status_mod.read_pid(directory)
    if isinstance(pid, int) and not status_mod.pid_alive(pid):
        # Re-read: the runner may have finished between the two reads.
        document = status_mod.read_document(directory / status_mod.STATUS_JSON) or document
        result = status_mod.result_from_document(document)
        if result.state == "running":
            tail = build_mod.log_tail(directory / status_mod.RUNNER_LOG)
            result.state = "error"
            result.message = f"verify runner (pid {pid}) exited without finishing" + (f":\n{tail}" if tail else "")
    return result


# Runners this process started. Polling them reaps the exited ones, so a
# finished runner does not linger as a zombie that still looks alive.
_LAUNCHED: dict[str, subprocess.Popen] = {}
_LAUNCHED_LOCK = threading.Lock()


def _reap() -> None:
    with _LAUNCHED_LOCK:
        for key, process in list(_LAUNCHED.items()):
            if process.poll() is not None:
                del _LAUNCHED[key]
