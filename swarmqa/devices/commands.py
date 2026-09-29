"""The command-runner seam shared by the device pools.

`runner(args, **kwargs)` is the only way a pool runs a host command. Tests
pass a fake that returns recorded output. `run` never raises for a failing
command: a missing binary becomes exit 127 and a timeout exit 124.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable

Runner = Callable[..., Any]


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def detail(self) -> str:
        text = (self.stderr or self.stdout).strip()
        return text or f"{' '.join(self.args)} exited {self.returncode}"


def default_runner(
    args: list[str],
    *,
    background: bool = False,
    timeout: float | None = None,
    **_: Any,
) -> Any:
    """Run a host command, or start it detached when `background` is set."""
    if background:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return subprocess.CompletedProcess(args, 0, "", "")
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def run(runner: Runner, args: list[str], **kwargs: Any) -> CommandResult:
    """Call `runner` and normalise its result."""
    try:
        raw = runner(list(args), **kwargs)
    except FileNotFoundError as exc:
        return CommandResult(list(args), 127, "", f"{exc.filename or args[0]} not found")
    except subprocess.TimeoutExpired:
        return CommandResult(list(args), 124, "", f"{' '.join(args)} timed out")
    if raw is None:
        return CommandResult(list(args), 0)
    return CommandResult(
        list(args),
        int(getattr(raw, "returncode", 0) or 0),
        _text(getattr(raw, "stdout", "")),
        _text(getattr(raw, "stderr", "")),
    )
