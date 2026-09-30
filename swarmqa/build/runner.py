"""The command runner the builder uses for every git, xcodebuild and shell call.

It follows the `swarmqa.devices.commands` seam: `runner(args, **kwargs)`
returns something with `returncode`, `stdout` and `stderr`, and
`commands.run` normalises it (a missing binary is exit 127, a timeout 124).
The builder passes these keyword arguments:

- `cwd`: the directory to run in.
- `env`: extra environment variables, merged over the current environment.
- `timeout`: seconds before the whole process group is killed.
- `log_path`: append stdout and stderr to this file instead of capturing them
  (xcodebuild output is large); `stdout` then comes back empty.

Tests pass a fake with the same shape that returns recorded output.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from swarmqa.devices.commands import CommandResult, Runner, run

__all__ = ["CommandResult", "Runner", "build_runner", "run"]


def build_runner(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    log_path: str | Path | None = None,
    **_: Any,
) -> subprocess.CompletedProcess:
    """Run `args`; kill its process group and raise TimeoutExpired after `timeout`."""
    merged = dict(os.environ)
    merged.update(env or {})
    if log_path is None:
        process = subprocess.Popen(
            args, cwd=cwd, env=merged, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True,
        )
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            raise
        return subprocess.CompletedProcess(args, process.returncode, out, err)
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            args, cwd=cwd, env=merged, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            log.write(f"\n# timed out after {timeout:g}s\n")
            raise
    return subprocess.CompletedProcess(args, code, "", "")


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
