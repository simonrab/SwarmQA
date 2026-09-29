"""The build step of a verify run.

`build_app` runs `app.build_command` through the shell in `app.source_dir`
(or the working directory), with a timeout, and logs to a file. It is the
one function a per-SHA builder (WP-C4) replaces: anything with the same
signature returning a `BuildOutcome` works as `builder` in `core.verify`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from swarmqa.models import CampaignConfig

BUILD_TIMEOUT_S = 1800.0
LOG_TAIL_LINES = 20


@dataclass
class BuildOutcome:
    """`ok` is False only when a build ran and failed or timed out."""

    ok: bool
    skipped: bool = False
    message: str = ""
    log: str | None = None
    duration_s: float = 0.0


Builder = Callable[[CampaignConfig, Path], BuildOutcome]


def build_app(config: CampaignConfig, log_path: Path, *, timeout_s: float | None = None) -> BuildOutcome:
    """Run the configured build command, or skip with a note when there is none."""
    command = (config.app.build_command or "").strip()
    if not command:
        return BuildOutcome(ok=True, skipped=True, message="no app.build_command; replayed the existing build")
    timeout = BUILD_TIMEOUT_S if timeout_s is None else timeout_s
    cwd = Path(config.app.source_dir).expanduser() if config.app.source_dir else Path.cwd()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {command}\n# cwd: {cwd}\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.write(f"# could not start: {exc}\n")
            return BuildOutcome(ok=False, message=f"build could not start: {exc}", log=str(log_path))
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            log.write(f"\n# timed out after {timeout:g}s\n")
            code = None
    duration = time.monotonic() - started
    tail = log_tail(log_path)
    if code is None:
        message = f"build timed out after {timeout:g}s"
    elif code != 0:
        message = f"build failed (exit {code})"
    else:
        return BuildOutcome(ok=True, message="build succeeded", log=str(log_path), duration_s=duration)
    if tail:
        message += ":\n" + tail
    return BuildOutcome(ok=False, message=message, log=str(log_path), duration_s=duration)


def log_tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
